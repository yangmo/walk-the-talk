"""Phase 3 verify skeleton tests：premature 短路 + IO 链路 + resume + filter。

不打真实 LLM，验证：
1. PREMATURE 短路逻辑正确（horizon.end > current_fy）
2. 不可解析 horizon 走 agent；本测试注入"always not_verifiable" stub LLM 模拟无证据场景
3. current_fiscal_year 显式覆盖优先
4. resume 跳过已验证 claim
5. claim_ids / years 过滤
6. claims.json 缺失抛友好错误
7. VerdictStore JSON round-trip

本文件不验证 agent 内部决策（那在 test_verify_agent.py），只验证 pipeline
层的串接与 IO；agent 路径用极简 stub LLM（一次 plan→finalize→not_verifiable）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from walk_the_talk.config import VerifySettings
from walk_the_talk.core.enums import (
    ClaimType,
    SectionCanonical,
    StatementType,
    Verdict,
)
from walk_the_talk.core.models import (
    Claim,
    ClaimStore,
    FinancialLine,
    Horizon,
    Predicate,
    Subject,
    VerdictStore,
)
from walk_the_talk.ingest.embedding import make_embedder
from walk_the_talk.ingest.financials_store import FinancialsStore
from walk_the_talk.ingest.reports_store import ReportsStore
from walk_the_talk.llm import LLMClient, LLMResponse
from walk_the_talk.verify.pipeline import (
    VerifyResult,
    _detect_current_fiscal_year_from_store,
    _load_reports_store,
    _parse_fy,
    run_verify,
)

# ============== Stub LLM：一律 finalize → not_verifiable ==============


class _AlwaysNotVerifiableLLM(LLMClient):
    """plan 总是返回 finalize；finalize 总是返回 not_verifiable。

    用于跑 pipeline IO/串接测试，不关心 agent 内部决策细节。
    """

    name = "stub-not-verifiable"

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages,
        *,
        model,
        temperature=0.0,
        max_tokens=None,
        response_format=None,
        timeout=60.0,
    ) -> LLMResponse:
        self.calls += 1
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        if "plan 阶段" in last_user:
            text = json.dumps(
                {"action": "finalize", "rationale": "stub: 直接收尾"},
                ensure_ascii=False,
            )
        else:
            text = json.dumps(
                {
                    "verdict": "not_verifiable",
                    "actual_value": None,
                    "confidence": 0.0,
                    "comment": "stub: skeleton 测试不评估证据",
                    "evidence_chunk_ids": [],
                },
                ensure_ascii=False,
            )
        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


def _run_verify(settings: VerifySettings, **kw: Any) -> VerifyResult:
    """所有 skeleton 测试统一注入 stub LLM，避免触发真实 DeepSeek 构造。"""
    return run_verify(settings, llm=_AlwaysNotVerifiableLLM(), **kw)


# ============== Helpers ==============


def _make_claim(
    *,
    claim_id: str = "000001-FY2024-001",
    from_fy: int = 2024,
    end_fy: str = "FY2025",
    start_fy: str = "FY2025",
    metric_canonical: str = "revenue",
    section: SectionCanonical = SectionCanonical.MDA,
) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=ClaimType.QUANTITATIVE_FORECAST,
        section="管理层讨论与分析",
        section_canonical=section,
        original_text=f"预计 {start_fy}-{end_fy} 收入增长 10%",
        locator=f"{claim_id}#1",
        subject=Subject(scope="整体"),
        metric="营业收入",
        metric_canonical=metric_canonical,
        predicate=Predicate(operator=">=", value=0.10, unit="同比"),
        horizon=Horizon(type="财年", start=start_fy, end=end_fy),
        from_fiscal_year=from_fy,
        canonical_key=f"{metric_canonical}|整体|{start_fy}~{end_fy}",
    )


def _seed_environment(
    tmp_path: Path,
    claims: list[Claim],
    *,
    periods: list[int] = (2024, 2025),
    ticker: str = "000001",
    company: str = "测试公司",
) -> Path:
    """构造最小的 _walk_the_talk/ 工作目录：claims.json + financials.db。"""

    work = tmp_path / "_walk_the_talk"
    work.mkdir(parents=True, exist_ok=True)

    store = ClaimStore(
        company_name=company,
        ticker=ticker,
        years_processed=sorted({c.from_fiscal_year for c in claims}),
        claims={c.claim_id: c for c in claims},
    )
    (work / "claims.json").write_text(
        store.model_dump_json(indent=2), encoding="utf-8"
    )

    fs = FinancialsStore(work / "financials.db")
    try:
        fs.upsert_lines(
            [
                FinancialLine(
                    ticker=ticker,
                    fiscal_period=f"FY{y}",
                    statement_type=StatementType.INCOME,
                    line_item="营业收入",
                    line_item_canonical="revenue",
                    value=1.0e9,
                )
                for y in periods
            ]
        )
    finally:
        fs.close()
    return work


# ============== Unit: _parse_fy ==============


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        ("FY2024", 2024),
        ("FY1999", 1999),
        ("  FY2030  ", 2030),
        ("长期", None),
        ("滚动期", None),
        ("", None),
        (None, None),
        ("2024", None),  # 必须带 FY 前缀
    ],
)
def test_parse_fy(s, expected) -> None:
    assert _parse_fy(s) == expected


# ============== Unit: _detect_current_fiscal_year_from_store ==============


def test_detect_current_fiscal_year_from_store_picks_max(tmp_path: Path) -> None:
    """乱序 ingest 的财年也应取最大值。"""
    work = _seed_environment(
        tmp_path, [_make_claim()], periods=[2022, 2023, 2025, 2024]
    )
    with FinancialsStore(work / "financials.db") as store:
        assert _detect_current_fiscal_year_from_store(store, "000001") == 2025


def test_detect_current_fiscal_year_wrong_ticker_raises(tmp_path: Path) -> None:
    """ticker 在 DB 里没数据 → 友好错误，提示 --current-fy 兜底。"""
    work = _seed_environment(tmp_path, [_make_claim()], periods=[2024])
    with FinancialsStore(work / "financials.db") as store:
        with pytest.raises(RuntimeError, match="999999"):
            _detect_current_fiscal_year_from_store(store, "999999")


def test_run_verify_missing_db_raises(tmp_path: Path) -> None:
    """没 ingest 过 → run_verify 友好错误，提示先跑 ingest。"""
    # 直接放一份 claims.json（无 financials.db），让 run_verify 走到检测阶段
    claim = _make_claim(claim_id="000001-FY2024-001", from_fy=2024, end_fy="FY2025")
    work = tmp_path / "_walk_the_talk"
    work.mkdir(parents=True, exist_ok=True)
    store = ClaimStore(
        company_name="测试公司",
        ticker="000001",
        years_processed=[2024],
        claims={claim.claim_id: claim},
    )
    (work / "claims.json").write_text(store.model_dump_json(indent=2), encoding="utf-8")

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    with pytest.raises(RuntimeError, match="financials.db"):
        _run_verify(settings)


# ============== Pipeline ==============


def test_premature_short_circuit(tmp_path: Path) -> None:
    """horizon.end > current_fy → PREMATURE。"""

    claim = _make_claim(claim_id="000001-FY2024-001", from_fy=2024, end_fy="FY2026")
    _seed_environment(tmp_path, [claim], periods=[2024, 2025])

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    result = _run_verify(settings)

    assert result.claims_total == 1
    assert result.claims_processed == 1
    assert result.current_fiscal_year == 2025
    assert result.verdicts_by_type.get("premature") == 1
    assert result.verdicts_by_type.get("not_verifiable") is None

    store = VerdictStore.model_validate_json(
        settings.verdicts_path.read_text(encoding="utf-8")
    )
    rec = store.verifications["000001-FY2024-001"][0]
    assert rec.verdict == Verdict.PREMATURE
    assert rec.confidence == 1.0
    assert "FY2026" in rec.comment
    assert "FY2025" in rec.comment


def test_not_premature_falls_to_stub(tmp_path: Path) -> None:
    """horizon.end ≤ current_fy → stub NOT_VERIFIABLE。"""

    claim = _make_claim(claim_id="000001-FY2024-002", from_fy=2024, end_fy="FY2025")
    _seed_environment(tmp_path, [claim], periods=[2024, 2025])

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    result = _run_verify(settings)

    assert result.verdicts_by_type.get("not_verifiable") == 1
    assert result.verdicts_by_type.get("premature") is None


def test_unparseable_horizon_falls_to_stub(tmp_path: Path) -> None:
    """horizon.end='长期' 不解析 → 不短路 → stub。"""

    claim = _make_claim(claim_id="000001-FY2024-003", from_fy=2024, end_fy="长期")
    _seed_environment(tmp_path, [claim], periods=[2024, 2025])

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    result = _run_verify(settings)

    assert result.verdicts_by_type.get("not_verifiable") == 1


def test_current_fiscal_year_override(tmp_path: Path) -> None:
    """显式 --current-fy 优先于 financials.db 自动检测。"""

    claim = _make_claim(claim_id="000001-FY2024-004", from_fy=2024, end_fy="FY2025")
    _seed_environment(tmp_path, [claim], periods=[2024, 2025])

    # 强制 current_fy=2024，则 FY2025 变 PREMATURE
    settings = VerifySettings(
        data_dir=tmp_path,
        ticker="000001",
        company="测试公司",
        current_fiscal_year=2024,
    )
    result = _run_verify(settings)

    assert result.current_fiscal_year == 2024
    assert result.verdicts_by_type.get("premature") == 1


def test_resume_skips_existing(tmp_path: Path) -> None:
    """resume=True 第二次跑跳过已验证。"""

    claim = _make_claim(claim_id="000001-FY2024-005", from_fy=2024, end_fy="FY2026")
    _seed_environment(tmp_path, [claim], periods=[2024, 2025])

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    result1 = _run_verify(settings)
    assert result1.claims_processed == 1
    assert result1.claims_skipped == 0

    result2 = _run_verify(settings)
    assert result2.claims_processed == 0
    assert result2.claims_skipped == 1


def test_no_resume_overwrites(tmp_path: Path) -> None:
    """resume=False 重新打 verdict。"""

    claim = _make_claim(claim_id="000001-FY2024-006", from_fy=2024, end_fy="FY2026")
    _seed_environment(tmp_path, [claim], periods=[2024, 2025])

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    _run_verify(settings)

    settings_no_resume = VerifySettings(
        data_dir=tmp_path, ticker="000001", company="测试公司", resume=False
    )
    result = _run_verify(settings_no_resume)
    assert result.claims_processed == 1
    assert result.claims_skipped == 0


def test_claim_ids_filter(tmp_path: Path) -> None:
    """--claim-ids 只验证指定 ID。"""

    claims = [
        _make_claim(claim_id=f"000001-FY2024-{i:03d}", end_fy="FY2026")
        for i in (1, 2, 3)
    ]
    _seed_environment(tmp_path, claims, periods=[2024, 2025])

    settings = VerifySettings(
        data_dir=tmp_path,
        ticker="000001",
        company="测试公司",
        claim_ids=["000001-FY2024-002"],
    )
    result = _run_verify(settings)

    assert result.claims_total == 3            # 总数包含全部
    assert result.claims_processed == 1        # 但只处理 1 条


def test_years_filter(tmp_path: Path) -> None:
    """--years 只验证指定发出年的 claim。"""

    claims = [
        _make_claim(claim_id="000001-FY2023-001", from_fy=2023, end_fy="FY2026"),
        _make_claim(claim_id="000001-FY2024-001", from_fy=2024, end_fy="FY2026"),
    ]
    _seed_environment(tmp_path, claims, periods=[2024, 2025])

    settings = VerifySettings(
        data_dir=tmp_path,
        ticker="000001",
        company="测试公司",
        years=[2024],
    )
    result = _run_verify(settings)

    assert result.claims_processed == 1
    assert result.verdicts_by_year == {2024: 1}


def test_no_claims_file_raises(tmp_path: Path) -> None:
    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    with pytest.raises(RuntimeError, match="claims.json"):
        _run_verify(settings)


def test_verdict_store_json_round_trip(tmp_path: Path) -> None:
    """verdicts.json 写出后能完整 round-trip。"""

    claim = _make_claim(claim_id="000001-FY2024-007", from_fy=2024, end_fy="FY2026")
    _seed_environment(tmp_path, [claim], periods=[2024, 2025])

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    _run_verify(settings)

    raw = json.loads(settings.verdicts_path.read_text(encoding="utf-8"))
    assert raw["company_name"] == "测试公司"
    assert raw["ticker"] == "000001"
    assert raw["claims_processed"] == ["000001-FY2024-007"]
    assert "000001-FY2024-007" in raw["verifications"]

    # Pydantic round-trip
    store = VerdictStore.model_validate(raw)
    assert store.verifications["000001-FY2024-007"][0].verdict == Verdict.PREMATURE


def test_mixed_claims_summary(tmp_path: Path) -> None:
    """混合 claim 类型 → verdicts_by_type / verdicts_by_year 汇总正确。"""

    claims = [
        _make_claim(claim_id="000001-FY2023-001", from_fy=2023, end_fy="FY2025"),  # stub
        _make_claim(claim_id="000001-FY2024-001", from_fy=2024, end_fy="FY2026"),  # premature
        _make_claim(claim_id="000001-FY2024-002", from_fy=2024, end_fy="FY2027"),  # premature
        _make_claim(claim_id="000001-FY2024-003", from_fy=2024, end_fy="长期"),    # stub
    ]
    _seed_environment(tmp_path, claims, periods=[2024, 2025])

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="测试公司")
    result = _run_verify(settings)

    assert result.claims_total == 4
    assert result.claims_processed == 4
    assert result.verdicts_by_type == {"premature": 2, "not_verifiable": 2}
    assert result.verdicts_by_year == {2023: 1, 2024: 3}


# ============== _load_reports_store：embedder 自动检测 ==============


def _seed_reports_collection(
    work_dir: Path, *, ticker: str, embedder_name: str
) -> None:
    """用指定 embedder 建一个 reports_<ticker> collection（写一条 dummy chunk）。"""
    from walk_the_talk.core.enums import ReportType, SectionCanonical
    from walk_the_talk.core.models import Chunk

    embedder = make_embedder(embedder_name)
    store = ReportsStore(persist_dir=work_dir, ticker=ticker, embedder=embedder)
    store.add_chunks(
        [
            Chunk(
                chunk_id=f"{ticker}-FY2024-mda-p001",
                ticker=ticker,
                fiscal_period="FY2024",
                report_type=ReportType.ANNUAL,
                section="管理层讨论与分析",
                section_canonical=SectionCanonical.MDA,
                source_path="/tmp/2024.html",
                locator="管理层讨论与分析#1",
                text="测试文本，用于验证 embedder 维度。",
            )
        ]
    )


def test_load_reports_store_auto_detect_hash(tmp_path: Path) -> None:
    """ingest 用 hash → verify 自动检测出 hash → 不撞维度。"""
    work = tmp_path / "_walk_the_talk"
    work.mkdir()
    _seed_reports_collection(work, ticker="000001", embedder_name="hash")

    settings = VerifySettings(data_dir=tmp_path, ticker="000001", company="x")
    store = _load_reports_store(settings)
    assert store is not None
    assert store.embedder.name == "hash"
    # 真正发个查询验证维度对得上（不会抛 InvalidArgumentError）
    hits = store.query_dense("测试", k=1)
    assert len(hits) == 1


def test_load_reports_store_explicit_override(tmp_path: Path) -> None:
    """settings.embedder 显式指定 → 优先于 metadata 检测。"""
    work = tmp_path / "_walk_the_talk"
    work.mkdir()
    _seed_reports_collection(work, ticker="000001", embedder_name="hash")

    # 故意 override 成 hash（即使有 metadata 也走 override 路径）
    settings = VerifySettings(
        data_dir=tmp_path, ticker="000001", company="x", embedder="hash"
    )
    store = _load_reports_store(settings)
    assert store is not None
    assert store.embedder.name == "hash"


def test_load_reports_store_unknown_collection_falls_to_bge(tmp_path: Path) -> None:
    """chroma_dir 存在但 collection 不存在 → 回落 bge 默认。

    注意：不实际加载 BGE 模型（lazy-init），只看 embedder.name。
    """
    work = tmp_path / "_walk_the_talk"
    chroma = work / "chroma"
    chroma.mkdir(parents=True)
    # 不调 _seed_reports_collection，让 collection 不存在

    settings = VerifySettings(data_dir=tmp_path, ticker="999999", company="x")
    store = _load_reports_store(settings)
    assert store is not None
    assert store.embedder.name == "bge-small-zh-v1.5"
