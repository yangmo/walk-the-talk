"""可信度评分公式（纯函数，独立可测）。

锁定决策（design_p1234.md §七）：

1. partially_verified 权重 = 0.5（偏严格，宁可低估不高估）
2. v1 不按 claim_type 加权；所有 claim 平权进总分
3. NOT_VERIFIABLE / PREMATURE / EXPIRED 不进分母（不惩罚数据缺失，不预先打分）

公式：score = sum(weight) / |actionable| * 100（四舍五入到整数百分比）。
actionable = {VERIFIED, PARTIALLY_VERIFIED, FAILED}

子集评分（quantitative_hit_rate / capital_alloc_accuracy）只是把 actionable
进一步限定到某一 claim_type，公式不变。
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.enums import ClaimType, Verdict
from ..core.models import Claim, VerificationRecord

# ============== 常量 ==============

_VERDICT_WEIGHTS: dict[str, float] = {
    Verdict.VERIFIED.value: 1.0,
    Verdict.PARTIALLY_VERIFIED.value: 0.5,
    Verdict.FAILED.value: 0.0,
}

# 进入分母的 verdict 集合
_ACTIONABLE_VERDICTS: frozenset[str] = frozenset(_VERDICT_WEIGHTS.keys())


# ============== 主函数 ==============


def overall_credibility(records: Iterable[VerificationRecord]) -> int | None:
    """整体可信度 0-100；分母只算 V/P/F；全为 NV/PR/EXP 时返回 None。

    返回 None 表示"无可对照 claim"，调用方应在报告里显式说明。
    """
    actionable = [r for r in records if r.verdict.value in _ACTIONABLE_VERDICTS]
    if not actionable:
        return None
    score = sum(_VERDICT_WEIGHTS[r.verdict.value] for r in actionable) / len(actionable)
    return round(score * 100)


def claim_type_hit_rate(
    records: Iterable[VerificationRecord],
    claims: dict[str, Claim],
    claim_type: ClaimType,
    *,
    record_owner: dict[VerificationRecord, str] | None = None,
) -> int | None:
    """指定 claim_type 子集的命中率（与 overall_credibility 同公式，但子集过滤）。

    record_owner: 可选 {VerificationRecord: claim_id} 映射；如未提供，要求调用方
    传入的 records 是已经过滤好的 list。两种调用模式都支持，方便测试。
    """
    if record_owner is None:
        # 无映射：调用方应自行过滤后传入
        actionable = [r for r in records if r.verdict.value in _ACTIONABLE_VERDICTS]
    else:
        actionable = []
        for r in records:
            cid = record_owner.get(r)
            if cid is None:
                continue
            c = claims.get(cid)
            if c is None or c.claim_type != claim_type:
                continue
            if r.verdict.value not in _ACTIONABLE_VERDICTS:
                continue
            actionable.append(r)
    if not actionable:
        return None
    score = sum(_VERDICT_WEIGHTS[r.verdict.value] for r in actionable) / len(actionable)
    return round(score * 100)


def quantitative_hit_rate(
    verifications: dict[str, list[VerificationRecord]],
    claims: dict[str, Claim],
) -> int | None:
    """quantitative_forecast 子集命中率。"""
    return _subset_score(verifications, claims, ClaimType.QUANTITATIVE_FORECAST)


def capital_alloc_accuracy(
    verifications: dict[str, list[VerificationRecord]],
    claims: dict[str, Claim],
) -> int | None:
    """capital_allocation 子集命中率。"""
    return _subset_score(verifications, claims, ClaimType.CAPITAL_ALLOCATION)


def _subset_score(
    verifications: dict[str, list[VerificationRecord]],
    claims: dict[str, Claim],
    target_type: ClaimType,
) -> int | None:
    """通用子集评分实现：按 claim_type 过滤后用 overall_credibility 公式。"""
    selected: list[VerificationRecord] = []
    for cid, recs in verifications.items():
        c = claims.get(cid)
        if c is None or c.claim_type != target_type:
            continue
        # 若一个 claim 多次验证，取最近一次（fiscal_year 最大）作为最终结论
        latest = max(recs, key=lambda r: r.fiscal_year, default=None)
        if latest is not None:
            selected.append(latest)
    return overall_credibility(selected)


def verdict_distribution(
    records: Iterable[VerificationRecord],
) -> dict[str, int]:
    """统计 6 种 verdict 的频次（缺席的 verdict 也补 0）。"""
    counts = {v.value: 0 for v in Verdict}
    for r in records:
        counts[r.verdict.value] = counts.get(r.verdict.value, 0) + 1
    return counts


def latest_verdict_per_claim(
    verifications: dict[str, list[VerificationRecord]],
) -> dict[str, VerificationRecord]:
    """每个 claim 取 fiscal_year 最大的那次验证作为最终结论。"""
    out: dict[str, VerificationRecord] = {}
    for cid, recs in verifications.items():
        if not recs:
            continue
        out[cid] = max(recs, key=lambda r: r.fiscal_year)
    return out
