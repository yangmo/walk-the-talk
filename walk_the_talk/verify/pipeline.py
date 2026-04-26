"""Phase 3 verify 编排：claims.json + financials.db → verdicts.json。

主流程：
- 加载 claims.json
- 检测 current_fiscal_year（来自 financials.db 最新 FY，或 CLI 显式覆盖）
- 对每条 claim：
    * horizon.end > current_fy → PREMATURE（短路，不调 LLM）
    * 其他 → run_agent(...) 跑 LangGraph 验证（plan ↔ tool ↔ finalize）
- 落盘 verdicts.json（VerdictStore schema）
- 支持 resume：已存在 verdict 的 claim 跳过

Agent 依赖三件事：
- LLMClient：用 DeepSeekClient（带 PromptCache）；测试时可注入 stub。
- FinancialsStore：sqlite，agent 调 query_financials 走它。
- ChunkSearcher（ReportsStore）：可选；agent 调 query_chunks 时用，缺则被工具拒绝。

主入口：
    run_verify(settings, on_log=None, *, llm=None, reports_store=None) -> VerifyResult

llm/reports_store 可选注入（测试用），生产路径会按 settings 自己构造。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..config import VerifySettings
from ..core.enums import Verdict
from ..core.models import (
    Claim,
    ClaimStore,
    VerdictStore,
    VerificationRecord,
)
from ..ingest.financials_store import FinancialsStore
from ..llm import LLMClient
from .agent import run_agent
from .tools import ChunkSearcher, list_derived_canonicals

log = logging.getLogger(__name__)

_FY_RE = re.compile(r"FY(\d{4})")


# ============== Result ==============


@dataclass
class VerifyResult:
    """verify 跑批的统计快照，给 CLI 渲染表格用。"""

    claims_total: int = 0                # claims.json 里全部 claim 数
    claims_processed: int = 0            # 本次实际产出 verdict 的 claim 数
    claims_skipped: int = 0              # resume 跳过的 claim 数
    claims_failed: list[str] = field(default_factory=list)
    current_fiscal_year: int = 0
    verdicts_by_type: dict[str, int] = field(default_factory=dict)
    verdicts_by_year: dict[int, int] = field(default_factory=dict)
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tool_calls_total: int = 0
    elapsed_seconds: float = 0.0


# ============== 入口 ==============


def run_verify(
    settings: VerifySettings,
    *,
    on_log: Callable[[str], None] | None = None,
    llm: LLMClient | None = None,
    reports_store: ChunkSearcher | None = None,
    financials_store: FinancialsStore | None = None,
) -> VerifyResult:
    """执行一次 verify 跑批。

    llm / reports_store / financials_store 留作可选注入：
        - 测试时塞 stub
        - 生产路径走 settings：DeepSeek + PromptCache + 真 SQLite/Chroma
    """

    logger = on_log or (lambda msg: None)
    result = VerifyResult()
    t0 = time.time()

    # 1. 校验 + 加载 claims.json
    if not settings.claims_path.exists():
        raise RuntimeError(
            f"claims.json 不存在：{settings.claims_path}；先跑 walk-the-talk extract"
        )

    claim_store = ClaimStore.model_validate_json(
        settings.claims_path.read_text(encoding="utf-8")
    )
    all_claims = list(claim_store.claims.values())
    result.claims_total = len(all_claims)
    logger(f"[dim]loaded[/] claims.json：{len(all_claims)} 条 claim")

    # 2. 选筛 claim_ids / years
    selected = _filter_claims(all_claims, settings)
    if len(selected) != len(all_claims):
        logger(f"[dim]filter[/] {len(all_claims)} → {len(selected)} 条")

    # 3. 准备 stores（financials 必需；reports 可选）
    own_financials = False
    if financials_store is None:
        if not settings.financials_db_path.exists():
            raise RuntimeError(
                f"financials.db 不存在：{settings.financials_db_path}；先跑 ingest"
            )
        financials_store = FinancialsStore(settings.financials_db_path)
        own_financials = True

    if reports_store is None and settings.chroma_dir.exists():
        try:
            reports_store = _load_reports_store(settings, logger=logger)
        except Exception as e:  # noqa: BLE001
            log.warning("加载 ReportsStore 失败（query_chunks 将不可用）：%s", e)
            reports_store = None

    try:
        # 4. 检测 current_fiscal_year
        if settings.current_fiscal_year is not None:
            current_fy = settings.current_fiscal_year
            logger(f"[dim]current_fy[/] FY{current_fy} (CLI override)")
        else:
            current_fy = _detect_current_fiscal_year_from_store(
                financials_store, settings.ticker
            )
            logger(f"[dim]current_fy[/] FY{current_fy} (来自 financials.db)")
        result.current_fiscal_year = current_fy

        # 4.5 一次性取 financials canonical 白名单，注入 system prompt
        # （每次 verify 跑批只查一次，传给所有 claim 的 agent）
        # 白名单 = DB 直查的基础 canonical ∪ tools.py 里定义的派生字段（gross_margin 等）。
        # LLM 只能从这个集合里挑名字传给 query_financials；query_financials 自己识别派生
        # 还是基础并分别走对应分支。
        try:
            base_canonicals = financials_store.list_canonicals(settings.ticker)
        except Exception as e:  # noqa: BLE001
            log.warning("list_canonicals 失败：%s；白名单未注入", e)
            base_canonicals = []
        derived_canonicals = list_derived_canonicals()
        available_canonicals = sorted(set(base_canonicals) | set(derived_canonicals))
        logger(
            f"[dim]canonicals[/] {len(available_canonicals)} 项已注入"
            f"（基础 {len(base_canonicals)} + 派生 {len(derived_canonicals)}）"
        )

        # 5. resume：加载已有 verdicts.json
        if settings.resume and settings.verdicts_path.exists():
            verdict_store = VerdictStore.model_validate_json(
                settings.verdicts_path.read_text(encoding="utf-8")
            )
            logger(
                f"[dim]resume[/] 已有 verdicts.json："
                f"{len(verdict_store.verifications)} 条 claim 已验证"
            )
        else:
            verdict_store = VerdictStore(
                company_name=settings.company,
                ticker=settings.ticker,
            )
        already_verified = set(verdict_store.verifications.keys())

        # 6. 准备 LLM（agent 路径需要；PREMATURE 短路不需要）
        # 留 None，等真要进 agent 时再 lazy 构造，避免 PREMATURE-only 跑批白拿 API key。
        agent_llm: LLMClient | None = llm

        # 7. 逐条处理
        for claim in selected:
            if settings.resume and claim.claim_id in already_verified:
                result.claims_skipped += 1
                continue

            try:
                # PREMATURE 短路（无 LLM）
                end_year = _parse_fy(claim.horizon.end)
                if end_year is not None and end_year > current_fy:
                    record = _build_premature_record(claim, current_fy=current_fy)
                else:
                    if agent_llm is None:
                        agent_llm = _build_default_llm(settings)
                    record = _verify_with_agent(
                        claim,
                        llm=agent_llm,
                        financials_store=financials_store,
                        reports_store=reports_store,
                        current_fy=current_fy,
                        settings=settings,
                        available_canonicals=available_canonicals,
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("verify failed for %s", claim.claim_id)
                logger(f"[red]✗ {claim.claim_id}: {type(e).__name__}: {e}[/]")
                result.claims_failed.append(claim.claim_id)
                continue

            verdict_store.verifications.setdefault(claim.claim_id, []).append(record)
            if claim.claim_id not in verdict_store.claims_processed:
                verdict_store.claims_processed.append(claim.claim_id)

            result.claims_processed += 1
            result.verdicts_by_type[record.verdict.value] = (
                result.verdicts_by_type.get(record.verdict.value, 0) + 1
            )
            result.verdicts_by_year[claim.from_fiscal_year] = (
                result.verdicts_by_year.get(claim.from_fiscal_year, 0) + 1
            )
            result.tool_calls_total += len(record.computation_trace)
            cost = record.cost or {}
            result.prompt_tokens += int(cost.get("prompt_tokens", 0) or 0)
            result.completion_tokens += int(cost.get("completion_tokens", 0) or 0)
            result.total_tokens += int(cost.get("total_tokens", 0) or 0)
            result.cache_hits += int(cost.get("cache_hits", 0) or 0)

        # 8. 落盘
        settings.verdicts_path.parent.mkdir(parents=True, exist_ok=True)
        settings.verdicts_path.write_text(
            verdict_store.model_dump_json(indent=2),
            encoding="utf-8",
        )
        logger(f"[dim]wrote[/] {settings.verdicts_path}")

        result.elapsed_seconds = time.time() - t0
        return result
    finally:
        if own_financials:
            financials_store.close()
        # ReportsStore / DeepSeekClient 暂无显式 close 接口；交给 GC


# ============== 单条 claim 处理 ==============


def _verify_with_agent(
    claim: Claim,
    *,
    llm: LLMClient,
    financials_store: FinancialsStore,
    reports_store: ChunkSearcher | None,
    current_fy: int,
    settings: VerifySettings,
    available_canonicals: list[str] | None = None,
) -> VerificationRecord:
    """走 LangGraph agent 跑一条 claim。"""
    agent_result = run_agent(
        claim,
        llm=llm,
        financials_store=financials_store,
        reports_store=reports_store,
        current_fiscal_year=current_fy,
        chat_model=settings.chat_model,
        reasoner_model=settings.reasoner_model,
        max_iters=settings.max_iters,
        ticker=settings.ticker,
        available_canonicals=available_canonicals,
    )
    return agent_result.record


def _build_premature_record(
    claim: Claim, *, current_fy: int
) -> VerificationRecord:
    return VerificationRecord(
        fiscal_year=current_fy,
        verdict=Verdict.PREMATURE,
        target_value=claim.predicate.value,
        actual_value=None,
        evidence=[],
        computation_trace=[],
        confidence=1.0,
        comment=(
            f"horizon end ({claim.horizon.end}) 大于当前财年 (FY{current_fy})；"
            f"预测窗口尚未到达，无法验证。"
        ),
        cost={},
    )


# ============== 工具 ==============


def _parse_fy(s: str | None) -> int | None:
    """从 'FY2024' 等字符串解析整年；不匹配返回 None（含 '长期' / '滚动期' 等）。"""
    if not s:
        return None
    m = _FY_RE.match(s.strip())
    return int(m.group(1)) if m else None


def _filter_claims(claims: Iterable[Claim], settings: VerifySettings) -> list[Claim]:
    out = list(claims)
    if settings.claim_ids:
        ids = set(settings.claim_ids)
        out = [c for c in out if c.claim_id in ids]
    if settings.years:
        yrs = set(settings.years)
        out = [c for c in out if c.from_fiscal_year in yrs]
    return out


def _detect_current_fiscal_year_from_store(
    store: FinancialsStore, ticker: str
) -> int:
    """从已打开的 FinancialsStore 推断当前财年（该 ticker 出现过的最大 FY）。"""
    periods = store.list_periods(ticker)
    years = [y for y in (_parse_fy(p) for p in periods) if y is not None]
    if not years:
        raise RuntimeError(
            f"financials.db 里没有 ticker={ticker} 的数据；用 --current-fy 显式覆盖或先跑 ingest"
        )
    return max(years)


def _build_default_llm(settings: VerifySettings) -> LLMClient:
    """生产路径默认 LLM 客户端：DeepSeek + PromptCache。"""
    from ..llm import DeepSeekClient, PromptCache

    cache = PromptCache(settings.llm_cache_path)
    return DeepSeekClient(cache=cache)


def _load_reports_store(
    settings: VerifySettings,
    *,
    logger: Callable[[str], None] = lambda _msg: None,
) -> ChunkSearcher | None:
    """自动加载 ReportsStore：用 ingest 时记录在 chroma collection metadata 里的 embedder。

    ingest 创建 collection 时把 embedder.name 塞在 metadata（见 reports_store.py），
    这里读出来按名字 make_embedder，避免维度不匹配（hash 256 vs bge 512）。

    settings.embedder 显式优先于 metadata 自动检测。
    检测失败时按"ingest 默认 = bge"兜底。
    """
    from ..ingest.embedding import make_embedder
    from ..ingest.reports_store import ReportsStore

    chosen = (settings.embedder or "").strip() or None
    detected: str | None = None

    if chosen is None:
        try:
            import chromadb

            client = chromadb.PersistentClient(path=str(settings.chroma_dir))
            coll_name = f"{ReportsStore.COLLECTION_PREFIX}{settings.ticker}"
            try:
                coll = client.get_collection(name=coll_name)
                meta = coll.metadata or {}
                detected = str(meta.get("embedder") or "").strip() or None
            except Exception as e:  # noqa: BLE001
                log.debug("chroma get_collection 失败：%s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("peek chroma metadata 失败，回落 bge：%s", e)

    embedder_name = chosen or detected or "bge"

    try:
        embedder = make_embedder(embedder_name)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "make_embedder(%r) 失败（%s），回落 hash；query_chunks 维度可能不匹配",
            embedder_name,
            e,
        )
        embedder = make_embedder("hash")

    store = ReportsStore(
        persist_dir=settings.chroma_dir.parent,
        ticker=settings.ticker,
        embedder=embedder,
    )
    src = "CLI override" if chosen else ("collection metadata" if detected else "default=bge")
    logger(
        f"[dim]reports_store[/] 已加载（chunks={store.count()}，"
        f"embedder={embedder.name} via {src}）"
    )
    return store
