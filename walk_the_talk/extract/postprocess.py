"""Phase 2 后处理：去重 + 过滤 + 兜底。

输入：单年（或多年混合）的 list[Claim]
输出：经过滤、按 claim_id 唯一的 list[Claim]，附统计 dict

步骤：
    1. section 黑名单兜底（LEGAL_TEMPLATE / NOTES / ESG / GOVERNANCE / SHARES）
    2. horizon 时效过滤：horizon.end < from_fiscal_year → 抽到了当期事实，丢弃
    3. trivial 阈值：specificity_score ≤ 2 且 materiality_score ≤ 2 → 丢弃
    4. 同年 canonical_key 去重：留 specificity 最高的（并列时留 extraction_confidence 最高）
    5. 跨年法律样板指纹去重：original_text 完全相同的多年记录只留首年
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..core.enums import SectionCanonical
from ..core.ids import text_fingerprint
from ..core.models import Claim

# section 黑名单：这些 canonical 下抽出来的 claim 视为噪声
_SECTION_BLACKLIST: frozenset[SectionCanonical] = frozenset(
    {
        SectionCanonical.LEGAL_TEMPLATE,
        SectionCanonical.NOTES,
        SectionCanonical.ESG,
        SectionCanonical.GOVERNANCE,
        SectionCanonical.SHARES,
    }
)

# horizon.end 的简易解析：FY2025 / 2025 / 2025年 / FY2025-FY2030 ...
_FY_RE = re.compile(r"FY?\s*(\d{4})", re.IGNORECASE)


@dataclass
class PostprocessStats:
    input_count: int = 0
    dropped_section_blacklist: int = 0
    dropped_expired: int = 0
    dropped_trivial: int = 0
    dedup_within_year: int = 0
    dedup_cross_year: int = 0
    output_count: int = 0


def postprocess_claims(
    claims: Iterable[Claim],
    *,
    trivial_specificity_max: int = 2,
    trivial_materiality_max: int = 2,
) -> tuple[list[Claim], PostprocessStats]:
    """运行完整后处理链。"""
    stats = PostprocessStats()
    survivors: list[Claim] = []

    # 步骤 1-3：单条过滤
    for c in claims:
        stats.input_count += 1
        if c.section_canonical in _SECTION_BLACKLIST:
            stats.dropped_section_blacklist += 1
            continue
        if _is_expired(c):
            stats.dropped_expired += 1
            continue
        if (
            c.specificity_score <= trivial_specificity_max
            and c.materiality_score <= trivial_materiality_max
        ):
            stats.dropped_trivial += 1
            continue
        survivors.append(c)

    # 步骤 4：同年 canonical_key 去重
    survivors, n_dropped_within = _dedup_within_year(survivors)
    stats.dedup_within_year = n_dropped_within

    # 步骤 5：跨年法律样板指纹去重（同一段 original_text 多年重复出现）
    survivors, n_dropped_cross = _dedup_cross_year_template(survivors)
    stats.dedup_cross_year = n_dropped_cross

    stats.output_count = len(survivors)
    return survivors, stats


# ============== 内部工具 ==============


def _is_expired(c: Claim) -> bool:
    """horizon.end 早于（严格小于）from_fiscal_year ⇒ 抽到了当期/历史事实。"""
    end_year = _parse_fy(c.horizon.end)
    if end_year is None:
        return False  # 无法解析就不过滤，留给人工 / 后续阶段
    return end_year < c.from_fiscal_year


def _parse_fy(s: str) -> int | None:
    if not s:
        return None
    m = _FY_RE.search(s)
    if not m:
        # 兜底：纯 4 位数字
        m = re.search(r"\b(20\d{2})\b", s)
        if not m:
            return None
    return int(m.group(1))


def _dedup_within_year(claims: list[Claim]) -> tuple[list[Claim], int]:
    """以 (from_fiscal_year, canonical_key) 为 key 聚合，留 specificity 最高的。"""
    if not claims:
        return [], 0
    groups: dict[tuple[int, str], list[Claim]] = {}
    for c in claims:
        groups.setdefault((c.from_fiscal_year, c.canonical_key), []).append(c)

    kept: list[Claim] = []
    dropped = 0
    for (_, _), grp in groups.items():
        if len(grp) == 1:
            kept.append(grp[0])
            continue
        # 排序：specificity 高优先；并列 extraction_confidence 高优先
        grp.sort(
            key=lambda x: (x.specificity_score, x.extraction_confidence),
            reverse=True,
        )
        kept.append(grp[0])
        dropped += len(grp) - 1
    # 维持稳定输出顺序：按 claim_id
    kept.sort(key=lambda x: x.claim_id)
    return kept, dropped


def _dedup_cross_year_template(claims: list[Claim]) -> tuple[list[Claim], int]:
    """跨年完全重复的 original_text 视为模板，只留出现在最早年份的那条。"""
    if not claims:
        return [], 0
    seen: dict[str, Claim] = {}
    for c in claims:
        # 用 original_text + canonical_key 联合指纹，避免误杀（不同 metric 的同段引文）
        key = text_fingerprint(c.original_text + "|" + c.canonical_key, length=20)
        if key not in seen or c.from_fiscal_year < seen[key].from_fiscal_year:
            seen[key] = c
    kept = list(seen.values())
    kept.sort(key=lambda x: x.claim_id)
    return kept, len(claims) - len(kept)
