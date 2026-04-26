"""各 section 的渲染函数。返回 markdown string。

入参全是已过滤好的纯 Python 数据（Claim / VerificationRecord / int），
不接触 IO，便于测试。
"""

from __future__ import annotations

from collections import defaultdict

from ..core.models import Claim, VerificationRecord
from . import templates as T
from .highlights import HighlightItem

# ============== 评分板 ==============


def render_scoreboard(
    overall: int | None,
    quantitative: int | None,
    capital_alloc: int | None,
) -> str:
    """3 行评分表。某子集 None 时显示"—"且备注解释为什么没分。"""
    rows = []
    rows.append(
        _score_row(
            "整体可信度",
            overall,
            "(verified*1.0 + partially_verified*0.5 + failed*0.0) / (V+P+F) × 100",
        )
    )
    rows.append(
        _score_row(
            "量化承诺命中率",
            quantitative,
            "quantitative_forecast 类型 claim 子集，公式同上",
        )
    )
    rows.append(
        _score_row(
            "资本配置准确度",
            capital_alloc,
            "capital_allocation 类型 claim 子集，公式同上",
        )
    )
    body = T.SCOREBOARD_HEADER + "".join(rows)
    if overall is None:
        body += T.SCOREBOARD_NO_DATA_NOTE
    return body


def _score_row(dim: str, score: int | None, note: str) -> str:
    score_str = "—" if score is None else f"**{score}**"
    return T.SCOREBOARD_ROW.format(dim=dim, score=score_str, note=note)


# ============== 历年简史 ==============


def render_timeline(
    pairs: list[tuple[Claim, VerificationRecord]],
    *,
    max_items_per_bucket: int = 8,
) -> str:
    """按 from_fiscal_year 倒序分组；每年 6 个 bucket。

    每个 bucket 最多列 max_items_per_bucket 条；超出显示"... 还有 N 条"。
    """
    if not pairs:
        return T.TIMELINE_HEADER + "\n*(no claims to render)*\n"

    by_year: dict[int, list[tuple[Claim, VerificationRecord]]] = defaultdict(list)
    for c, r in pairs:
        by_year[c.from_fiscal_year].append((c, r))

    parts: list[str] = [T.TIMELINE_HEADER]
    for fy in sorted(by_year.keys(), reverse=True):
        parts.append(T.YEAR_BLOCK_HEADER.format(fy=fy))
        # 按 verdict 分桶
        buckets: dict[str, list[tuple[Claim, VerificationRecord]]] = defaultdict(list)
        for c, r in by_year[fy]:
            buckets[r.verdict.value].append((c, r))
        # 桶内排序：materiality_score 降序，再按 claim_id
        for k in buckets:
            buckets[k].sort(key=lambda x: (-x[0].materiality_score, x[0].claim_id))

        for verdict_key, label in T.BUCKET_ORDER:
            items = buckets.get(verdict_key, [])
            if not items:
                continue
            parts.append(T.BUCKET_HEADER.format(emoji_label=label, n=len(items)))
            for c, r in items[:max_items_per_bucket]:
                parts.append(
                    T.BUCKET_ITEM.format(
                        cid=c.claim_id,
                        summary=_claim_summary(c, r),
                    )
                )
            if len(items) > max_items_per_bucket:
                parts.append(f"  - ... 另有 {len(items) - max_items_per_bucket} 条同类\n")
    return "".join(parts)


def _claim_summary(claim: Claim, record: VerificationRecord) -> str:
    """单条 claim 的 markdown 行内摘要。"""
    text = (claim.original_text or "").strip().replace("\n", " ")
    # 截断过长原文
    if len(text) > 80:
        text = text[:77] + "..."
    target = _fmt_value(record.target_value)
    actual = _fmt_value(record.actual_value)
    parts = [f'"{text}"']
    if target is not None and actual is not None:
        parts.append(f"目标 {target} / 实际 {actual}")
    elif actual is not None:
        parts.append(f"实际 {actual}")
    if record.comment:
        comment = record.comment.replace("\n", " ").strip()
        if len(comment) > 80:
            comment = comment[:77] + "..."
        parts.append(comment)
    return " — ".join(parts)


def _fmt_value(v: object) -> str | None:
    """把 target/actual_value 渲染成短字符串。None / 空字符串返回 None。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        # 大数自动换亿/万
        if abs(v) >= 1e8:
            return f"{v / 1e8:.2f}亿"
        if abs(v) >= 1e4:
            return f"{v / 1e4:.2f}万"
        # 小数（百分比、比率）
        if isinstance(v, float) and abs(v) < 10:
            return f"{v:.2%}" if abs(v) < 1 else f"{v:.2f}"
        return str(v)
    return str(v)


# ============== 突出事件 ==============


def render_highlights(
    failed: list[HighlightItem],
    verified: list[HighlightItem],
    premature: list[HighlightItem],
) -> str:
    """突出事件区。三组都为空时返回空字符串（让 builder 整段省略）。"""
    if not failed and not verified and not premature:
        return ""
    parts: list[str] = [T.HIGHLIGHTS_HEADER]

    if failed:
        parts.append(T.HIGHLIGHT_FAILED_HEADER)
        for item in failed:
            parts.append(_render_highlight_item(item))
    if verified:
        parts.append(T.HIGHLIGHT_VERIFIED_HEADER)
        for item in verified:
            parts.append(_render_highlight_item(item))
    if premature:
        parts.append(T.HIGHLIGHT_PREMATURE_HEADER)
        for item in premature:
            parts.append(_render_highlight_item(item))
    return "".join(parts)


def _render_highlight_item(item: HighlightItem) -> str:
    summary = _claim_summary(item.claim, item.record)
    suffix = ""
    if item.anomaly_detail:
        suffix = T.HIGHLIGHT_ANOMALY_SUFFIX.format(detail=item.anomaly_detail)
    return T.HIGHLIGHT_ITEM.format(
        cid=item.claim.claim_id,
        summary=summary,
        anomaly_suffix=suffix,
    )


# ============== 验证方法 ==============


def render_method_note(current_fy: int) -> str:
    return T.METHOD_TPL.format(current_fy=current_fy)
