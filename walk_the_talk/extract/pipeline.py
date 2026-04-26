"""Extract phase 编排：把已 ingest 完的年报 chunks → claims.json。

线性外环（按年），并发内环（per-chunk ThreadPoolExecutor）。
进度落 `<data_dir>/_walk_the_talk/_progress.json` 的 `claims` phase。

主入口：
    run_extract(settings, llm_client=None, on_log=None, debug=False) -> ExtractResult

诊断输出：
    始终：每年按 section_canonical 列 chunks / raw_claims / final_claims 的小表
    debug=True：额外落 claims.raw.json（postprocess 前）+ extract_log.jsonl（per-chunk）
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ExtractSettings
from ..core.ids import claim_id as build_claim_id
from ..core.ids import fiscal_period as build_fiscal_period
from ..core.models import Chunk, Claim, ClaimStore
from ..ingest.embedding import make_embedder
from ..ingest.pipeline import ProgressTracker
from ..ingest.reports_store import ReportsStore
from ..llm import DeepSeekClient, LLMClient, PromptCache
from .extractor import extract_from_chunk
from .postprocess import postprocess_claims

log = logging.getLogger(__name__)

CLAIMS_PHASE = "claims"
RAW_CLAIMS_FILENAME = "claims.raw.json"
EXTRACT_LOG_FILENAME = "extract_log.jsonl"

# Pre-LLM trivial chunk filter
TRIVIAL_MIN_CHARS = 100
_TABLE_PLACEHOLDER_RE = re.compile(r"\[\[TABLE_PLACEHOLDER_\d+\]\]")


def _is_trivial_chunk(chunk: Chunk, min_chars: int = TRIVIAL_MIN_CHARS) -> bool:
    """删掉表格占位符 + 所有空白后字符数 < min_chars → 视为 trivial 不送 LLM。

    覆盖：纯表格占位符、表头残片、"√适用 □不适用"模板字符等。
    """
    text = _TABLE_PLACEHOLDER_RE.sub("", chunk.text or "")
    text = re.sub(r"\s+", "", text)
    return len(text) < min_chars


# ============== Result ==============


@dataclass
class ExtractResult:
    years_processed: list[int] = field(default_factory=list)
    years_skipped: list[int] = field(default_factory=list)
    chunks_total: int = 0
    raw_claims_total: int = 0
    final_claims_total: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    fallback_used: int = 0
    chunks_failed: int = 0
    chunks_skipped_trivial: int = 0
    # 诊断：累计 per-section 计数
    chunks_by_section: dict[str, int] = field(default_factory=dict)
    raw_claims_by_section: dict[str, int] = field(default_factory=dict)
    final_claims_by_section: dict[str, int] = field(default_factory=dict)
    # 诊断：postprocess 各步骤丢弃数
    pp_dropped_blacklist: int = 0
    pp_dropped_expired: int = 0
    pp_dropped_trivial: int = 0
    pp_dedup_within_year: int = 0
    pp_dedup_cross_year: int = 0


# ============== 主流程 ==============


def run_extract(
    settings: ExtractSettings,
    *,
    llm_client: LLMClient | None = None,
    on_log: Any = None,
    debug: bool = False,
) -> ExtractResult:
    """跑 extract phase。线程不安全。"""

    settings.work_dir.mkdir(parents=True, exist_ok=True)

    def _emit(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            log.info(msg)

    # LLM 客户端 + 缓存
    if llm_client is None:
        cache = PromptCache(settings.llm_cache_path)
        llm_client = DeepSeekClient(cache=cache)
        _emit(f"[llm] DeepSeek 已就绪，缓存：{settings.llm_cache_path}")

    # ReportsStore（embedder 不会被用到，给个 hash 占位）
    embedder = make_embedder("hash")
    reports_store = ReportsStore(
        persist_dir=settings.work_dir,
        ticker=settings.ticker,
        embedder=embedder,
    )

    # 进度
    progress = ProgressTracker(settings.progress_path, settings.ticker, settings.company)

    # 现有 claims.json（若有）
    existing = _load_claim_store(settings.claims_path, settings.ticker, settings.company)

    # debug 落盘准备
    raw_dump: dict[str, list[dict[str, Any]]] = {}  # year_str → raw claim dicts
    log_lines: list[str] = []  # extract_log.jsonl

    # 选定年份
    years = settings.years or _discover_years(progress)
    if not years:
        raise RuntimeError("没有可处理的年份。请先跑 `walk-the-talk ingest` 或显式 --years。")
    _emit(f"待处理年份：{years}（resume={settings.resume}, debug={debug}）")

    result = ExtractResult()

    for year in years:
        if settings.resume and progress.is_done(year, CLAIMS_PHASE) and _year_has_claims(existing, year):
            _emit(f"  [skip] {year}（claims phase 已完成）")
            result.years_skipped.append(year)
            continue

        # 非 resume 模式：先把该年旧 claims 从内存清掉
        _drop_year(existing, year)

        all_chunks = reports_store.iter_chunks(
            fiscal_periods=[build_fiscal_period(year)],
            section_canonicals=settings.section_canonicals,
        )
        all_chunks.sort(key=lambda c: c.chunk_id)
        if not all_chunks:
            _emit(f"  [warn] {year}: 没有候选 chunk（可能 ingest 还没跑过该年）")
            continue

        # Pre-LLM trivial filter：过滤表格占位符 / 极短残片 / 模板字符
        chunks: list[Chunk] = []
        skipped_trivial = 0
        for c in all_chunks:
            if _is_trivial_chunk(c):
                skipped_trivial += 1
                continue
            chunks.append(c)

        if skipped_trivial:
            _emit(
                f"  [{year}] pre-LLM filter: skipped {skipped_trivial} trivial "
                f"chunks (<{TRIVIAL_MIN_CHARS} chars after stripping placeholders/whitespace)"
            )
        result.chunks_skipped_trivial += skipped_trivial

        if not chunks:
            _emit(f"  [warn] {year}: 全部候选 chunk 都被 trivial filter 砍掉了")
            continue

        # 按 section 统计 chunk 数（filter 后）
        chunks_sec = Counter(str(c.section_canonical) for c in chunks)
        for s, n in chunks_sec.items():
            result.chunks_by_section[s] = result.chunks_by_section.get(s, 0) + n
        _emit(
            f"  [{year}] 候选 chunk: {len(chunks)} (filtered from {len(all_chunks)})    "
            f"by section: {dict(chunks_sec)}"
        )

        year_claims, stats, per_chunk_logs = _extract_year(
            chunks=chunks,
            year=year,
            client=llm_client,
            chat_model=settings.chat_model,
            reasoner_model=settings.reasoner_model,
            max_workers=settings.max_workers,
            on_log=_emit,
            collect_logs=debug,
        )

        # raw 统计 & 落盘准备
        raw_sec = Counter(str(c.section_canonical) for c in year_claims)
        for s, n in raw_sec.items():
            result.raw_claims_by_section[s] = result.raw_claims_by_section.get(s, 0) + n

        if debug:
            raw_dump[str(year)] = [c.model_dump(mode="json") for c in year_claims]
            log_lines.extend(json.dumps(r, ensure_ascii=False) for r in per_chunk_logs)

        # 后处理
        cleaned, pp_stats = postprocess_claims(year_claims)
        _emit(
            f"  [{year}] postprocess: in={pp_stats.input_count} "
            f"black={pp_stats.dropped_section_blacklist} expired={pp_stats.dropped_expired} "
            f"trivial={pp_stats.dropped_trivial} dedup_y={pp_stats.dedup_within_year} "
            f"dedup_x={pp_stats.dedup_cross_year} → out={pp_stats.output_count}"
        )
        result.pp_dropped_blacklist += pp_stats.dropped_section_blacklist
        result.pp_dropped_expired += pp_stats.dropped_expired
        result.pp_dropped_trivial += pp_stats.dropped_trivial
        result.pp_dedup_within_year += pp_stats.dedup_within_year
        result.pp_dedup_cross_year += pp_stats.dedup_cross_year

        # final per-section
        final_sec = Counter(str(c.section_canonical) for c in cleaned)
        for s, n in final_sec.items():
            result.final_claims_by_section[s] = result.final_claims_by_section.get(s, 0) + n

        # 写回 ClaimStore
        for c in cleaned:
            existing.claims[c.claim_id] = c
        if year not in existing.years_processed:
            existing.years_processed.append(year)
            existing.years_processed.sort()

        # 落盘 + 标记进度
        _save_claim_store(settings.claims_path, existing)
        progress.mark_done(year, CLAIMS_PHASE)

        # 累计
        result.years_processed.append(year)
        result.chunks_total += len(chunks)
        result.raw_claims_total += stats["raw_claims"]
        result.final_claims_total += pp_stats.output_count
        result.cache_hits += stats["cache_hits"]
        result.prompt_tokens += stats["prompt_tokens"]
        result.completion_tokens += stats["completion_tokens"]
        result.total_tokens += stats["total_tokens"]
        result.fallback_used += stats["fallback_used"]
        result.chunks_failed += stats["chunks_failed"]

    # debug 落盘
    if debug:
        raw_path = settings.work_dir / RAW_CLAIMS_FILENAME
        raw_path.write_text(json.dumps(raw_dump, ensure_ascii=False, indent=2), encoding="utf-8")
        _emit(f"[debug] raw claims (postprocess 前) → {raw_path}")
        log_path = settings.work_dir / EXTRACT_LOG_FILENAME
        log_path.write_text("\n".join(log_lines) + ("\n" if log_lines else ""), encoding="utf-8")
        _emit(f"[debug] per-chunk log → {log_path}（共 {len(log_lines)} 行）")

    return result


# ============== Inspect（不调 LLM） ==============


@dataclass
class InspectResult:
    years: list[int]
    chunks_by_year_section: dict[int, dict[str, int]]
    total_chunks: int


def inspect_chunks(
    settings: ExtractSettings,
    *,
    on_log: Any = None,
) -> InspectResult:
    """只读地按年×section_canonical 统计 chunk 数；零 LLM 成本。"""

    def _emit(msg: str) -> None:
        if on_log:
            on_log(msg)
        else:
            log.info(msg)

    embedder = make_embedder("hash")
    store = ReportsStore(
        persist_dir=settings.work_dir,
        ticker=settings.ticker,
        embedder=embedder,
    )
    progress = ProgressTracker(settings.progress_path, settings.ticker, settings.company)
    years = settings.years or _discover_years(progress)
    if not years:
        years = []
        _emit("未发现已 ingest 的年份。")

    result = InspectResult(years=years, chunks_by_year_section={}, total_chunks=0)
    for year in years:
        chunks = store.iter_chunks(
            fiscal_periods=[build_fiscal_period(year)],
            section_canonicals=None,  # 全量统计，不只候选 sections
        )
        sec_counter: Counter[str] = Counter(str(c.section_canonical) for c in chunks)
        result.chunks_by_year_section[year] = dict(sec_counter)
        result.total_chunks += len(chunks)
    return result


# ============== 单年抽取（含 ThreadPoolExecutor） ==============


def _extract_year(
    *,
    chunks: list[Chunk],
    year: int,
    client: LLMClient,
    chat_model: str,
    reasoner_model: str,
    max_workers: int,
    on_log: Any,
    collect_logs: bool = False,
) -> tuple[list[Claim], dict[str, int], list[dict[str, Any]]]:
    """对单年的 chunks 并发抽取，返回 (claims, stats, per_chunk_logs)。"""
    stats = {
        "raw_claims": 0,
        "cache_hits": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "fallback_used": 0,
        "chunks_failed": 0,
    }

    chunk_results: dict[int, tuple[list[Claim], dict[str, Any]]] = {}
    per_chunk_logs: list[dict[str, Any]] = []
    t0 = time.time()
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(
                extract_from_chunk,
                client,
                chunk,
                fiscal_year=year,
                seq_start=0,
                chat_model=chat_model,
                reasoner_model=reasoner_model,
            ): idx
            for idx, chunk in enumerate(chunks)
        }
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            chunk = chunks[idx]
            try:
                claims, s = fut.result()
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] extract 异常: %s", chunk.chunk_id, e)
                stats["chunks_failed"] += 1
                completed += 1
                if collect_logs:
                    per_chunk_logs.append(
                        {
                            "chunk_id": chunk.chunk_id,
                            "section_canonical": str(chunk.section_canonical),
                            "year": year,
                            "n_claims": 0,
                            "error": f"{type(e).__name__}: {e}",
                            "fallback_used": False,
                        }
                    )
                continue
            chunk_results[idx] = (claims, s)
            stats["raw_claims"] += len(claims)
            if s.get("cached"):
                stats["cache_hits"] += 1
            stats["prompt_tokens"] += s.get("prompt_tokens", 0)
            stats["completion_tokens"] += s.get("completion_tokens", 0)
            stats["total_tokens"] += s.get("total_tokens", 0)
            if s.get("fallback_used"):
                stats["fallback_used"] += 1
            if s.get("error"):
                stats["chunks_failed"] += 1
            completed += 1
            if collect_logs:
                per_chunk_logs.append(
                    {
                        "chunk_id": chunk.chunk_id,
                        "section_canonical": str(chunk.section_canonical),
                        "year": year,
                        "n_claims": len(claims),
                        "error": s.get("error"),
                        "fallback_used": bool(s.get("fallback_used")),
                        "used_model": s.get("used_model"),
                        "cached": bool(s.get("cached")),
                        "prompt_tokens": s.get("prompt_tokens", 0),
                        "completion_tokens": s.get("completion_tokens", 0),
                    }
                )
            if completed % 10 == 0 or completed == len(chunks):
                elapsed = time.time() - t0
                on_log(
                    f"    [{year}] {completed}/{len(chunks)} chunks 完成 "
                    f"(claims+={stats['raw_claims']} cached={stats['cache_hits']} "
                    f"failed={stats['chunks_failed']} {elapsed:.1f}s)"
                )

    # 按 chunk 顺序拼装并重排 claim_id
    ticker = chunks[0].ticker
    flat: list[Claim] = []
    seq = 1
    for idx in range(len(chunks)):
        if idx not in chunk_results:
            continue
        for c in chunk_results[idx][0]:
            new_id = build_claim_id(ticker, year, seq)
            seq += 1
            flat.append(c.model_copy(update={"claim_id": new_id}))
    return flat, stats, per_chunk_logs


# ============== 落盘 ==============


def _load_claim_store(path: Path, ticker: str, company: str) -> ClaimStore:
    if not path.exists():
        return ClaimStore(company_name=company, ticker=ticker, years_processed=[], claims={})
    try:
        data = json.loads(path.read_text("utf-8"))
        return ClaimStore.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning("claims.json 解析失败，重建: %s", e)
        return ClaimStore(company_name=company, ticker=ticker, years_processed=[], claims={})


def _save_claim_store(path: Path, store: ClaimStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        store.model_dump_json(indent=2),
        encoding="utf-8",
    )


def _year_has_claims(store: ClaimStore, year: int) -> bool:
    return year in store.years_processed


def _drop_year(store: ClaimStore, year: int) -> None:
    """删除该年所有 claim 与 years_processed 标记，便于全量重跑。"""
    drop_ids = [cid for cid, c in store.claims.items() if c.from_fiscal_year == year]
    for cid in drop_ids:
        store.claims.pop(cid, None)
    if year in store.years_processed:
        store.years_processed.remove(year)


def _discover_years(progress: ProgressTracker) -> list[int]:
    """从 _progress.json 找出 ingest index phase 已 done 的年份。"""
    out: list[int] = []
    for ystr, phases in progress.data.years.items():
        if phases.get("index") == "done":
            try:
                out.append(int(ystr))
            except ValueError:
                continue
    out.sort()
    return out
