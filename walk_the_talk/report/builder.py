"""主入口：合成 markdown 报告 + CLI 落盘。

build_report(claim_store, verdict_store, ...) -> str   # 纯函数
run_report(settings, on_log) -> dict                    # CLI 包装：读 JSON、写 .md
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from ..config import ReportSettings
from ..core.models import Claim, ClaimStore, VerdictStore, VerificationRecord
from . import sections, templates
from .highlights import (
    AnomalyChecker,
    MetricSeriesFetcher,
    pick_failed_highlights,
    pick_premature_highlights,
    pick_verified_highlights,
)
from .scoring import (
    capital_alloc_accuracy,
    latest_verdict_per_claim,
    overall_credibility,
    quantitative_hit_rate,
    verdict_distribution,
)

# ============== 主合成函数 ==============


def build_report(
    claim_store: ClaimStore,
    verdict_store: VerdictStore,
    *,
    current_fy: int | None = None,
    include_highlights: bool = True,
    include_method_note: bool = True,
    today: str | None = None,
    fetcher: MetricSeriesFetcher | None = None,
) -> str:
    """合成 markdown 报告字符串。纯函数，便于测试。

    current_fy=None 时从 verdicts 自动推算（取所有 record.fiscal_year 最大值）；
    若 verdicts 也没有任何 record，回落到 ClaimStore.years_processed 的最大值。
    fetcher: 可选 MetricSeriesFetcher，传入则启用 FAILED 条目"数据存疑"标注。
    """
    # 1. 推算 current_fy
    if current_fy is None:
        current_fy = _detect_current_fy(claim_store, verdict_store)

    # 2. 每个 claim 的最近一次验证
    latest = latest_verdict_per_claim(verdict_store.verifications)

    # 3. 准备 (Claim, Record) pairs；只看有 record 的 claim
    pairs: list[tuple[Claim, VerificationRecord]] = []
    for cid, rec in latest.items():
        c = claim_store.claims.get(cid)
        if c is None:
            # claim 已不存在但 verdict 还在 — 跳过（schema 异常态）
            continue
        pairs.append((c, rec))

    all_records = list(latest.values())

    # 4. 评分
    overall = overall_credibility(all_records)
    quant = quantitative_hit_rate(verdict_store.verifications, claim_store.claims)
    capital = capital_alloc_accuracy(verdict_store.verifications, claim_store.claims)

    # 5. verdict 分布
    dist = verdict_distribution(all_records)

    # 6. 各 section
    scoreboard_md = sections.render_scoreboard(overall, quant, capital)
    timeline_md = sections.render_timeline(pairs)

    if include_highlights:
        anomaly_checker = (
            AnomalyChecker(fetcher=fetcher, ticker=verdict_store.ticker) if fetcher is not None else None
        )
        failed_h = pick_failed_highlights(pairs, anomaly_checker=anomaly_checker)
        verified_h = pick_verified_highlights(pairs)
        premature_h = pick_premature_highlights(pairs)
        highlights_md = sections.render_highlights(failed_h, verified_h, premature_h)
    else:
        highlights_md = ""

    method_md = sections.render_method_note(current_fy) if include_method_note else ""

    # 7. 套总模板
    return templates.REPORT_TPL.format(
        company=claim_store.company_name,
        ticker=claim_store.ticker,
        today=today or date.today().isoformat(),
        current_fy=current_fy,
        n_claims=len(claim_store.claims),
        n_v=dist["verified"],
        n_p=dist["partially_verified"],
        n_f=dist["failed"],
        n_nv=dist["not_verifiable"],
        n_pr=dist["premature"],
        n_exp=dist["expired"],
        scoreboard_section=scoreboard_md,
        timeline_section=timeline_md,
        highlights_section=highlights_md,
        method_section=method_md,
    )


def _detect_current_fy(cs: ClaimStore, vs: VerdictStore) -> int:
    """优先从 verdict.fiscal_year 取最大值；回落到 claim_store.years_processed。"""
    fys: list[int] = []
    for recs in vs.verifications.values():
        for r in recs:
            fys.append(r.fiscal_year)
    if fys:
        return max(fys)
    if cs.years_processed:
        return max(cs.years_processed)
    return date.today().year - 1  # 兜底


# ============== CLI 包装 ==============


def run_report(
    settings: ReportSettings,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """CLI 入口：读 JSON、合成、写 .md，返回汇总 dict 给 cli.py 打印。"""
    log = on_log or (lambda _msg: None)

    if not settings.claims_path.exists():
        raise FileNotFoundError(f"claims.json 不存在：{settings.claims_path}（先跑 walk-the-talk extract）")
    if not settings.verdicts_path.exists():
        raise FileNotFoundError(
            f"verdicts.json 不存在：{settings.verdicts_path}（先跑 walk-the-talk verify）"
        )

    claim_store = _load_claim_store(settings.claims_path)
    verdict_store = _load_verdict_store(settings.verdicts_path)

    log(f"loaded claims = {len(claim_store.claims)}")
    log(f"loaded verifications (claim 数) = {len(verdict_store.verifications)}")

    # 可选 financials.db 作为 anomaly fetcher 数据源
    fetcher: MetricSeriesFetcher | None = None
    if settings.financials_db_path.exists():
        fetcher = _SqliteMetricFetcher(settings.financials_db_path)
        log("启用 anomaly check (financials.db)")
    else:
        log("financials.db 不存在，跳过 anomaly check")

    md = build_report(
        claim_store,
        verdict_store,
        current_fy=settings.current_fy,
        include_highlights=settings.include_highlights,
        include_method_note=settings.include_method_note,
        fetcher=fetcher,
    )

    settings.report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.report_path.write_text(md, encoding="utf-8")
    log(f"wrote {settings.report_path}")

    # 摘要
    latest = latest_verdict_per_claim(verdict_store.verifications)
    dist = verdict_distribution(latest.values())
    overall = overall_credibility(latest.values())
    return {
        "n_claims": len(claim_store.claims),
        "n_verified": dist["verified"],
        "n_partial": dist["partially_verified"],
        "n_failed": dist["failed"],
        "n_not_verifiable": dist["not_verifiable"],
        "n_premature": dist["premature"],
        "n_expired": dist["expired"],
        "overall_credibility": overall,
        "current_fy": settings.current_fy or _detect_current_fy(claim_store, verdict_store),
        "report_path": str(settings.report_path),
    }


def _load_claim_store(path: Path) -> ClaimStore:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ClaimStore.model_validate(data)


def _load_verdict_store(path: Path) -> VerdictStore:
    data = json.loads(path.read_text(encoding="utf-8"))
    return VerdictStore.model_validate(data)


# ============== Anomaly fetcher（SQLite 实现） ==============


class _SqliteMetricFetcher(MetricSeriesFetcher):
    """从 financials.db 取 (fiscal_period, value) 序列。

    用 line_item_canonical 匹配 claim.metric_canonical；只取合并报表
    (is_consolidated=1)，按 fiscal_period 升序返回。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def fetch(self, ticker: str, metric_canonical: str) -> list[tuple[str, float]]:
        if not metric_canonical:
            return []
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.execute(
                """
                SELECT fiscal_period, value
                FROM financial_lines
                WHERE ticker = ?
                  AND line_item_canonical = ?
                  AND is_consolidated = 1
                ORDER BY fiscal_period
                """,
                (ticker, metric_canonical),
            )
            return [(row[0], float(row[1])) for row in cur.fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()
