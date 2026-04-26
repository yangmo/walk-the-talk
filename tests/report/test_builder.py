"""build_report / sections / highlights 的端到端 smoke + 关键 section 校验。"""

from __future__ import annotations

from walk_the_talk.core.enums import ClaimStatus, ClaimType, SectionCanonical, Verdict
from walk_the_talk.core.models import (
    Claim,
    ClaimStore,
    Horizon,
    Predicate,
    VerdictStore,
    VerificationRecord,
)
from walk_the_talk.report.builder import build_report
from walk_the_talk.report.highlights import (
    AnomalyChecker,
    MetricSeriesFetcher,
    pick_failed_highlights,
    pick_premature_highlights,
    pick_verified_highlights,
)
from walk_the_talk.report.sections import (
    render_highlights,
    render_method_note,
    render_scoreboard,
    render_timeline,
)

# ============== fixture ==============


def _mk_claim(
    cid: str,
    ctype: ClaimType = ClaimType.QUANTITATIVE_FORECAST,
    fy: int = 2024,
    *,
    text: str = "",
    materiality: int = 3,
    specificity: int = 3,
    metric_canonical: str = "",
    horizon_end: str = "FY2025",
) -> Claim:
    return Claim(
        claim_id=cid,
        claim_type=ctype,
        section="管理层讨论",
        section_canonical=SectionCanonical.MDA,
        original_text=text or f"claim {cid} 原文",
        locator=f"loc-{cid}",
        predicate=Predicate(operator="=", value=0),
        horizon=Horizon(type="财年", start=horizon_end, end=horizon_end),
        from_fiscal_year=fy,
        canonical_key=f"key-{cid}",
        materiality_score=materiality,
        specificity_score=specificity,
        metric_canonical=metric_canonical,
        status=ClaimStatus.OPEN,
    )


def _mk_record(
    verdict: Verdict,
    fy: int = 2025,
    *,
    target=None,
    actual=None,
    comment: str = "",
) -> VerificationRecord:
    return VerificationRecord(
        fiscal_year=fy,
        verdict=verdict,
        target_value=target,
        actual_value=actual,
        comment=comment,
    )


def _mk_store(*claim_record_pairs: tuple[Claim, VerificationRecord]) -> tuple[ClaimStore, VerdictStore]:
    claims = {c.claim_id: c for c, _ in claim_record_pairs}
    verifications = {c.claim_id: [r] for c, r in claim_record_pairs}
    cs = ClaimStore(
        company_name="测试公司",
        ticker="999999",
        years_processed=sorted({c.from_fiscal_year for c in claims.values()}),
        claims=claims,
    )
    vs = VerdictStore(
        company_name="测试公司",
        ticker="999999",
        claims_processed=list(claims.keys()),
        verifications=verifications,
    )
    return cs, vs


# ============== build_report smoke ==============


def test_build_report_smoke_minimum_input() -> None:
    """最小输入：1 个 verified claim，build_report 不崩，含关键 section。"""
    c = _mk_claim("999999-FY2024-001", text="2025 年营收同比+10%")
    r = _mk_record(Verdict.VERIFIED, target=0.1, actual=0.12)
    cs, vs = _mk_store((c, r))

    md = build_report(cs, vs)

    assert "测试公司" in md
    assert "ticker: 999999" in md
    assert "综合可信度评分" in md
    assert "历年简史" in md
    assert "FY2024" in md
    assert "999999-FY2024-001" in md
    assert "100" in md  # overall = 100
    assert "## 验证方法说明" in md


def test_build_report_includes_failed_in_highlights() -> None:
    """FAILED claim 应出现在 ## 突出事件 · 大幅落空 区。"""
    c = _mk_claim("X-FY2024-002", text="FY2025 capex 与上一年持平", materiality=5)
    r = _mk_record(Verdict.FAILED, target=0.0, actual=0.099)
    cs, vs = _mk_store((c, r))

    md = build_report(cs, vs)
    assert "突出事件" in md
    assert "大幅落空" in md
    assert "X-FY2024-002" in md


def test_build_report_no_highlights_when_disabled() -> None:
    c = _mk_claim("Y-FY2024-001")
    r = _mk_record(Verdict.FAILED)
    cs, vs = _mk_store((c, r))

    md = build_report(cs, vs, include_highlights=False)
    assert "突出事件" not in md
    assert "大幅落空" not in md


def test_build_report_no_method_when_disabled() -> None:
    c = _mk_claim("Z-FY2024-001")
    r = _mk_record(Verdict.VERIFIED)
    cs, vs = _mk_store((c, r))

    md = build_report(cs, vs, include_method_note=False)
    assert "验证方法说明" not in md


def test_build_report_handles_only_premature() -> None:
    """全 PREMATURE 时整体可信度应 fall back 到 None，不应 0/0。"""
    c = _mk_claim("P-FY2024-001")
    r = _mk_record(Verdict.PREMATURE)
    cs, vs = _mk_store((c, r))

    md = build_report(cs, vs)
    # 整体可信度行用 "—" 占位
    assert "—" in md
    assert "暂无可对照打分" in md


def test_build_report_year_grouping_correct() -> None:
    """多年 claim：历年简史按 from_fiscal_year 倒序分组。"""
    c1 = _mk_claim("A-FY2022-001", fy=2022)
    c2 = _mk_claim("B-FY2024-001", fy=2024)
    c3 = _mk_claim("C-FY2023-001", fy=2023)
    cs, vs = _mk_store(
        (c1, _mk_record(Verdict.VERIFIED)),
        (c2, _mk_record(Verdict.FAILED)),
        (c3, _mk_record(Verdict.PARTIALLY_VERIFIED)),
    )

    md = build_report(cs, vs)
    # 在 timeline 区域内 FY2024 应在 FY2023 之前，FY2023 在 FY2022 之前
    timeline_start = md.index("## 历年简史")
    body = md[timeline_start:]
    pos_2024 = body.index("FY2024")
    pos_2023 = body.index("FY2023")
    pos_2022 = body.index("FY2022")
    assert pos_2024 < pos_2023 < pos_2022


def test_build_report_verdict_emoji_in_timeline() -> None:
    """6 种 verdict 的 emoji 标签都能正确渲染。"""
    pairs = [
        (_mk_claim("V-1"), _mk_record(Verdict.VERIFIED)),
        (_mk_claim("F-1"), _mk_record(Verdict.FAILED)),
        (_mk_claim("P-1"), _mk_record(Verdict.PARTIALLY_VERIFIED)),
        (_mk_claim("NV-1"), _mk_record(Verdict.NOT_VERIFIABLE)),
        (_mk_claim("PR-1"), _mk_record(Verdict.PREMATURE)),
        (_mk_claim("EX-1"), _mk_record(Verdict.EXPIRED)),
    ]
    cs, vs = _mk_store(*pairs)
    md = build_report(cs, vs)
    for token in ("✅", "❌", "⚠️", "❓", "⏳", "⏰"):
        assert token in md, f"missing emoji {token}"


def test_build_report_current_fy_auto_detect() -> None:
    """current_fy=None 时应取所有 record.fiscal_year 最大值。"""
    c = _mk_claim("M-1", fy=2022)
    r = _mk_record(Verdict.VERIFIED, fy=2026)
    cs, vs = _mk_store((c, r))
    md = build_report(cs, vs)
    assert "FY2026" in md


def test_build_report_current_fy_explicit_override() -> None:
    c = _mk_claim("M-1", fy=2022)
    r = _mk_record(Verdict.VERIFIED, fy=2026)
    cs, vs = _mk_store((c, r))
    md = build_report(cs, vs, current_fy=2099)
    assert "FY2099" in md


# ============== sections / highlights 单测 ==============


def test_render_scoreboard_with_no_data() -> None:
    md = render_scoreboard(None, None, None)
    assert "暂无可对照打分" in md
    assert "—" in md


def test_render_scoreboard_with_full_data() -> None:
    md = render_scoreboard(76, 82, 60)
    assert "**76**" in md
    assert "**82**" in md
    assert "**60**" in md


def test_render_timeline_empty() -> None:
    md = render_timeline([])
    assert "no claims to render" in md


def test_pick_failed_highlights_sorts_by_materiality() -> None:
    pairs = [
        (_mk_claim("F1", materiality=2), _mk_record(Verdict.FAILED)),
        (_mk_claim("F2", materiality=5), _mk_record(Verdict.FAILED)),
        (_mk_claim("F3", materiality=3), _mk_record(Verdict.FAILED)),
        (_mk_claim("V1"), _mk_record(Verdict.VERIFIED)),  # 非 FAILED 应过滤
    ]
    items = pick_failed_highlights(pairs, top_n=10)
    assert [it.claim.claim_id for it in items] == ["F2", "F3", "F1"]


def test_pick_verified_highlights_filters_low_specificity() -> None:
    pairs = [
        (_mk_claim("V1", specificity=5), _mk_record(Verdict.VERIFIED)),
        (_mk_claim("V2", specificity=2), _mk_record(Verdict.VERIFIED)),  # 应被过滤
        (_mk_claim("V3", specificity=4), _mk_record(Verdict.VERIFIED)),
    ]
    items = pick_verified_highlights(pairs)
    assert [it.claim.claim_id for it in items] == ["V1", "V3"]


def test_pick_premature_highlights_sorts_by_horizon_end() -> None:
    pairs = [
        (_mk_claim("P1", horizon_end="FY2027"), _mk_record(Verdict.PREMATURE)),
        (_mk_claim("P2", horizon_end="FY2025"), _mk_record(Verdict.PREMATURE)),
        (_mk_claim("P3", horizon_end="FY2026"), _mk_record(Verdict.PREMATURE)),
    ]
    items = pick_premature_highlights(pairs)
    assert [it.claim.claim_id for it in items] == ["P2", "P3", "P1"]


def test_anomaly_checker_flags_huge_gap() -> None:
    """实际值 96 亿 vs 近年均值 480 亿（差 5x） → 应标记。"""

    class FakeFetcher(MetricSeriesFetcher):
        def fetch(self, ticker: str, metric_canonical: str) -> list[tuple[str, float]]:
            return [
                ("FY2021", 360e8),
                ("FY2022", 500e8),
                ("FY2023", 580e8),  # 近 3 期均值约 480 亿
            ]

    claim = _mk_claim("FAIL-1", metric_canonical="revenue")
    rec = _mk_record(Verdict.FAILED, actual=96e8)
    checker = AnomalyChecker(fetcher=FakeFetcher(), ticker="999999")
    detail = checker.check(claim, rec)
    assert detail is not None
    assert "数据存疑" not in detail  # detail 是注解中的"明细"，不重复 prefix
    assert "5.0x" in detail or "5.1x" in detail or "x" in detail


def test_anomaly_checker_no_flag_for_normal_value() -> None:
    class FakeFetcher(MetricSeriesFetcher):
        def fetch(self, ticker: str, metric_canonical: str) -> list[tuple[str, float]]:
            return [("FY2022", 500e8), ("FY2023", 520e8)]

    claim = _mk_claim("OK-1", metric_canonical="revenue")
    rec = _mk_record(Verdict.FAILED, actual=540e8)  # 仅 +4%
    checker = AnomalyChecker(fetcher=FakeFetcher(), ticker="999999")
    assert checker.check(claim, rec) is None


def test_anomaly_checker_handles_empty_series() -> None:
    class EmptyFetcher(MetricSeriesFetcher):
        def fetch(self, ticker: str, metric_canonical: str) -> list[tuple[str, float]]:
            return []

    claim = _mk_claim("E-1", metric_canonical="revenue")
    rec = _mk_record(Verdict.FAILED, actual=100.0)
    checker = AnomalyChecker(fetcher=EmptyFetcher(), ticker="999999")
    assert checker.check(claim, rec) is None


def test_anomaly_checker_handles_missing_metric() -> None:
    """claim 没有 metric_canonical 时，跳过不报错。"""

    class FakeFetcher(MetricSeriesFetcher):
        def fetch(self, ticker: str, metric_canonical: str) -> list[tuple[str, float]]:
            raise AssertionError("不应被调用")

    claim = _mk_claim("X-1", metric_canonical="")  # 空
    rec = _mk_record(Verdict.FAILED, actual=100.0)
    checker = AnomalyChecker(fetcher=FakeFetcher(), ticker="999999")
    assert checker.check(claim, rec) is None


def test_render_highlights_empty_returns_empty_string() -> None:
    """三组都为空 → builder 整段省略。"""
    md = render_highlights([], [], [])
    assert md == ""


def test_render_method_note_includes_current_fy() -> None:
    md = render_method_note(2025)
    assert "FY2025" in md
    assert "评分" in md


# ============== 集成场景 ==============


def test_build_report_realistic_mix() -> None:
    """混合多种 verdict / 多年 claim / 不同 claim_type 的真实场景。"""
    pairs = [
        # FY2024 出的 claim，FY2025 报告里验证
        (
            _mk_claim(
                "R-FY2024-001",
                ctype=ClaimType.QUANTITATIVE_FORECAST,
                fy=2024,
                materiality=5,
                specificity=4,
                text="2025 年营收增速达到可比同业平均值",
            ),
            _mk_record(Verdict.FAILED, fy=2025, target=0.05, actual=-0.7876),
        ),
        (
            _mk_claim(
                "R-FY2024-002",
                ctype=ClaimType.CAPITAL_ALLOCATION,
                fy=2024,
                materiality=5,
                specificity=5,
                text="FY2025 capex 与上一年持平",
            ),
            _mk_record(Verdict.FAILED, fy=2025, target=0.0, actual=0.0988),
        ),
        (
            _mk_claim(
                "R-FY2023-001", ctype=ClaimType.QUANTITATIVE_FORECAST, fy=2023, materiality=4, specificity=4
            ),
            _mk_record(Verdict.VERIFIED, fy=2024),
        ),
        (
            _mk_claim(
                "R-FY2023-002", ctype=ClaimType.STRATEGIC_COMMITMENT, fy=2023, materiality=3, specificity=2
            ),
            _mk_record(Verdict.NOT_VERIFIABLE, fy=2024),
        ),
        (
            _mk_claim("R-FY2022-001", ctype=ClaimType.CAPITAL_ALLOCATION, fy=2022, materiality=4),
            _mk_record(Verdict.PARTIALLY_VERIFIED, fy=2023),
        ),
        # 在途
        (_mk_claim("R-FY2024-003", fy=2024, horizon_end="FY2026"), _mk_record(Verdict.PREMATURE, fy=2025)),
    ]
    cs, vs = _mk_store(*pairs)
    md = build_report(cs, vs)

    # 关键 section 全在
    for header in ("综合可信度评分", "历年简史", "突出事件", "验证方法说明"):
        assert header in md
    # 至少一条 FAILED highlight + 一条 VERIFIED highlight + 一条 PREMATURE
    assert "大幅落空" in md
    assert "信守承诺" in md
    assert "当前在途" in md
    # 评分应是 (1V + 0.5P + 0F + 0F)/4 = 37 或 38（取决于 R-FY2023-001 算 1，余 3 实算）
    # 实际 actionable: V=1, P=1, F=2 → (1 + 0.5)/4 = 37.5 → 38
    assert "**38**" in md or "**37**" in md
