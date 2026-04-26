"""scoring.py 纯函数单测。锁定决策（design §七）：

1. partially_verified 权重 0.5
2. v1 不按 claim_type 加权
3. NV/PR/EXP 不进分母
"""
from __future__ import annotations

from walk_the_talk.core.enums import ClaimStatus, ClaimType, SectionCanonical, Verdict
from walk_the_talk.core.models import Claim, Horizon, Predicate, VerificationRecord
from walk_the_talk.report.scoring import (
    capital_alloc_accuracy,
    latest_verdict_per_claim,
    overall_credibility,
    quantitative_hit_rate,
    verdict_distribution,
)

# ============== fixture ==============


def _mk_record(verdict: Verdict, fy: int = 2025) -> VerificationRecord:
    return VerificationRecord(fiscal_year=fy, verdict=verdict)


def _mk_claim(cid: str, ctype: ClaimType, fy: int = 2024) -> Claim:
    return Claim(
        claim_id=cid,
        claim_type=ctype,
        section="管理层讨论",
        section_canonical=SectionCanonical.MDA,
        original_text=f"claim {cid}",
        locator=f"loc-{cid}",
        predicate=Predicate(operator="=", value=0),
        horizon=Horizon(type="财年", start=f"FY{fy + 1}", end=f"FY{fy + 1}"),
        from_fiscal_year=fy,
        canonical_key=f"key-{cid}",
        status=ClaimStatus.OPEN,
    )


# ============== overall_credibility ==============


def test_overall_all_verified_returns_100() -> None:
    recs = [_mk_record(Verdict.VERIFIED)] * 5
    assert overall_credibility(recs) == 100


def test_overall_all_failed_returns_0() -> None:
    recs = [_mk_record(Verdict.FAILED)] * 4
    assert overall_credibility(recs) == 0


def test_overall_half_partial_uses_weight_05() -> None:
    """4 条 partially_verified → 4 * 0.5 / 4 = 0.5 → 50。"""
    recs = [_mk_record(Verdict.PARTIALLY_VERIFIED)] * 4
    assert overall_credibility(recs) == 50


def test_overall_mixed_v_p_f() -> None:
    """2 verified + 2 partial + 1 failed → (2*1 + 2*0.5 + 1*0)/5 = 0.6 → 60。"""
    recs = [
        _mk_record(Verdict.VERIFIED),
        _mk_record(Verdict.VERIFIED),
        _mk_record(Verdict.PARTIALLY_VERIFIED),
        _mk_record(Verdict.PARTIALLY_VERIFIED),
        _mk_record(Verdict.FAILED),
    ]
    assert overall_credibility(recs) == 60


def test_overall_empty_returns_none() -> None:
    assert overall_credibility([]) is None


def test_overall_only_premature_returns_none() -> None:
    """全是未到验证窗口 → 不可打分。"""
    recs = [_mk_record(Verdict.PREMATURE)] * 3
    assert overall_credibility(recs) is None


def test_overall_only_not_verifiable_returns_none() -> None:
    recs = [_mk_record(Verdict.NOT_VERIFIABLE)] * 2
    assert overall_credibility(recs) is None


def test_overall_only_expired_returns_none() -> None:
    recs = [_mk_record(Verdict.EXPIRED)] * 2
    assert overall_credibility(recs) is None


def test_overall_excludes_nv_pr_exp_from_denominator() -> None:
    """关键测试（决策 #1 #2 锁定）：NV/PR/EXP 不进分母。

    1 verified + 1 failed → 50 分；多加 10 条 NV/PR/EXP 不应改变结果。
    """
    base = [_mk_record(Verdict.VERIFIED), _mk_record(Verdict.FAILED)]
    noise = (
        [_mk_record(Verdict.NOT_VERIFIABLE)] * 5
        + [_mk_record(Verdict.PREMATURE)] * 3
        + [_mk_record(Verdict.EXPIRED)] * 2
    )
    assert overall_credibility(base) == 50
    assert overall_credibility(base + noise) == 50


def test_overall_rounding() -> None:
    """3 verified + 1 failed → 75；5 verified + 3 partial → 6.5/8=0.8125 → 81。

    用非 half-boundary 值避免 Python banker's rounding（round(62.5)==62）的边界歧义。
    """
    assert overall_credibility(
        [_mk_record(Verdict.VERIFIED)] * 3 + [_mk_record(Verdict.FAILED)]
    ) == 75
    assert overall_credibility(
        [_mk_record(Verdict.VERIFIED)] * 5
        + [_mk_record(Verdict.PARTIALLY_VERIFIED)] * 3
    ) == 81


# ============== claim_type 子集 ==============


def test_quantitative_hit_rate_filters_claim_type() -> None:
    """只算 quantitative_forecast 子集；其他 claim_type 不影响子集分。"""
    claims = {
        "C1": _mk_claim("C1", ClaimType.QUANTITATIVE_FORECAST),
        "C2": _mk_claim("C2", ClaimType.QUANTITATIVE_FORECAST),
        "C3": _mk_claim("C3", ClaimType.STRATEGIC_COMMITMENT),  # 不算
        "C4": _mk_claim("C4", ClaimType.CAPITAL_ALLOCATION),    # 不算
    }
    verifications = {
        "C1": [_mk_record(Verdict.VERIFIED)],
        "C2": [_mk_record(Verdict.FAILED)],
        "C3": [_mk_record(Verdict.VERIFIED)],   # 应被过滤
        "C4": [_mk_record(Verdict.VERIFIED)],   # 应被过滤
    }
    # quantitative 子集：C1 verified + C2 failed → 50
    assert quantitative_hit_rate(verifications, claims) == 50


def test_quantitative_hit_rate_returns_none_when_no_quant() -> None:
    """若 claims 里没有 quantitative_forecast 类型，返回 None。"""
    claims = {
        "C1": _mk_claim("C1", ClaimType.STRATEGIC_COMMITMENT),
    }
    verifications = {"C1": [_mk_record(Verdict.VERIFIED)]}
    assert quantitative_hit_rate(verifications, claims) is None


def test_capital_alloc_accuracy_filters_claim_type() -> None:
    claims = {
        "C1": _mk_claim("C1", ClaimType.CAPITAL_ALLOCATION),
        "C2": _mk_claim("C2", ClaimType.CAPITAL_ALLOCATION),
        "C3": _mk_claim("C3", ClaimType.QUANTITATIVE_FORECAST),
    }
    verifications = {
        "C1": [_mk_record(Verdict.VERIFIED)],
        "C2": [_mk_record(Verdict.PARTIALLY_VERIFIED)],
        "C3": [_mk_record(Verdict.FAILED)],
    }
    # capital_alloc 子集：1 verified + 1 partial → (1+0.5)/2 = 0.75 → 75
    assert capital_alloc_accuracy(verifications, claims) == 75


def test_subset_uses_latest_verification() -> None:
    """同一 claim 多次验证时，取 fiscal_year 最大那次。"""
    claims = {"C1": _mk_claim("C1", ClaimType.QUANTITATIVE_FORECAST)}
    verifications = {
        "C1": [
            _mk_record(Verdict.FAILED, fy=2023),
            _mk_record(Verdict.VERIFIED, fy=2025),       # 最新
            _mk_record(Verdict.PARTIALLY_VERIFIED, fy=2024),
        ]
    }
    # 应取 FY2025 verified → 100
    assert quantitative_hit_rate(verifications, claims) == 100


# ============== verdict_distribution / latest_verdict ==============


def test_verdict_distribution_counts_all_six() -> None:
    recs = [
        _mk_record(Verdict.VERIFIED),
        _mk_record(Verdict.VERIFIED),
        _mk_record(Verdict.FAILED),
        _mk_record(Verdict.NOT_VERIFIABLE),
    ]
    d = verdict_distribution(recs)
    assert d["verified"] == 2
    assert d["partially_verified"] == 0   # 缺席补 0
    assert d["failed"] == 1
    assert d["not_verifiable"] == 1
    assert d["premature"] == 0
    assert d["expired"] == 0
    # 6 个 key 全部存在
    assert set(d.keys()) == {v.value for v in Verdict}


def test_verdict_distribution_empty() -> None:
    d = verdict_distribution([])
    assert all(v == 0 for v in d.values())
    assert len(d) == 6


def test_latest_verdict_per_claim_picks_max_fy() -> None:
    verifications = {
        "C1": [
            _mk_record(Verdict.FAILED, fy=2023),
            _mk_record(Verdict.VERIFIED, fy=2025),
            _mk_record(Verdict.PARTIALLY_VERIFIED, fy=2024),
        ],
        "C2": [_mk_record(Verdict.NOT_VERIFIABLE, fy=2024)],
        "C3": [],  # 空 list 应被跳过
    }
    latest = latest_verdict_per_claim(verifications)
    assert set(latest.keys()) == {"C1", "C2"}
    assert latest["C1"].verdict == Verdict.VERIFIED
    assert latest["C1"].fiscal_year == 2025
    assert latest["C2"].verdict == Verdict.NOT_VERIFIABLE
