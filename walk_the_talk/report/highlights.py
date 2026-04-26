"""突出事件挑选规则。

design §三-B 与 §七-C 锁定：

1. 大幅落空（FAILED）：按 (claim.materiality_score 降序, fiscal_year 降序)；top-N
2. 信守承诺（VERIFIED）：specificity_score >= 3 才入选（避免吹定性 claim），
   按 (specificity_score 降序, materiality_score 降序)；top-N
3. 当前在途（PREMATURE）：按 horizon.target_year 升序

附加：FAILED 条目可选检测"数据存疑"标志（决策 #5）。简单规则：
- actual_value 与同 ticker 同 metric_canonical 近年数据数量级偏差 >5x → 标 ⚠️
- 缺数据时跳过（不报错）

financials_store 是可选依赖；不传就跳过 anomaly 检测。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.enums import Verdict
from ..core.models import Claim, VerificationRecord

# ============== 类型 ==============


@dataclass
class HighlightItem:
    """挑选出的一条 highlight，含原 claim/record 引用与可选 anomaly 注。"""

    claim: Claim
    record: VerificationRecord
    anomaly_detail: str | None = None  # 非 None 表示该条需要标 ⚠️数据存疑


# ============== 主函数 ==============


# top-N 默认上限
DEFAULT_TOP_N = 5
# 信守承诺挑选门槛：specificity_score >= 此值
VERIFIED_SPECIFICITY_THRESHOLD = 3
# 数据存疑：数量级偏差倍数阈值
ANOMALY_MAGNITUDE_RATIO = 5.0


def pick_failed_highlights(
    pairs: list[tuple[Claim, VerificationRecord]],
    *,
    top_n: int = DEFAULT_TOP_N,
    anomaly_checker: AnomalyChecker | None = None,
) -> list[HighlightItem]:
    """挑出 FAILED 高亮。pairs 是 (Claim, latest VerificationRecord) 列表。"""
    failed = [
        (c, r) for c, r in pairs if r.verdict == Verdict.FAILED
    ]
    failed.sort(
        key=lambda x: (x[0].materiality_score, x[1].fiscal_year),
        reverse=True,
    )
    out: list[HighlightItem] = []
    for c, r in failed[:top_n]:
        anomaly = anomaly_checker.check(c, r) if anomaly_checker else None
        out.append(HighlightItem(claim=c, record=r, anomaly_detail=anomaly))
    return out


def pick_verified_highlights(
    pairs: list[tuple[Claim, VerificationRecord]],
    *,
    top_n: int = DEFAULT_TOP_N,
) -> list[HighlightItem]:
    """挑出 VERIFIED 高亮。要求 specificity_score >= 3。"""
    verified = [
        (c, r)
        for c, r in pairs
        if r.verdict == Verdict.VERIFIED
        and c.specificity_score >= VERIFIED_SPECIFICITY_THRESHOLD
    ]
    verified.sort(
        key=lambda x: (x[0].specificity_score, x[0].materiality_score),
        reverse=True,
    )
    return [HighlightItem(claim=c, record=r) for c, r in verified[:top_n]]


def pick_premature_highlights(
    pairs: list[tuple[Claim, VerificationRecord]],
    *,
    top_n: int = DEFAULT_TOP_N,
) -> list[HighlightItem]:
    """挑出 PREMATURE 高亮（在途 claim）。按 horizon.end 升序（先到期者先出）。"""
    premature = [(c, r) for c, r in pairs if r.verdict == Verdict.PREMATURE]
    premature.sort(key=lambda x: _horizon_end_key(x[0]))
    return [HighlightItem(claim=c, record=r) for c, r in premature[:top_n]]


def _horizon_end_key(claim: Claim) -> tuple[int, str]:
    """horizon.end 解析失败时退化到原字符串字典序，避免崩。"""
    end = claim.horizon.end or ""
    # 形如 'FY2026' 取数字部分
    digits = "".join(ch for ch in end if ch.isdigit())
    if digits:
        try:
            return (int(digits), end)
        except ValueError:
            pass
    return (10**9, end)


# ============== 数据存疑检测 ==============


class AnomalyChecker:
    """检测 FAILED claim 的 actual_value 是否存在数量级异常。

    简单实现：对比同 ticker 同 metric_canonical 多个 fiscal_period 的 value，
    如果某一年 value 比最近年份均值差 >ANOMALY_MAGNITUDE_RATIO 倍则记为存疑。

    依赖一个轻量 query 函数 fetch_metric_series(ticker, metric_canonical) ->
    list[(fiscal_period, value)]。verify 阶段已经有类似工具，但这里为了解耦
    不直接 import；调用方自己注入 fetcher。
    """

    def __init__(
        self,
        fetcher: MetricSeriesFetcher,
        ticker: str,
        magnitude_ratio: float = ANOMALY_MAGNITUDE_RATIO,
    ) -> None:
        self.fetcher = fetcher
        self.ticker = ticker
        self.magnitude_ratio = magnitude_ratio

    def check(self, claim: Claim, record: VerificationRecord) -> str | None:
        """如果异常返回 markdown 友好的 detail；否则 None。"""
        metric = claim.metric_canonical or ""
        actual = record.actual_value
        if not metric or actual is None:
            return None
        try:
            actual_f = float(actual)
        except (TypeError, ValueError):
            return None
        try:
            series = self.fetcher.fetch(self.ticker, metric)
        except Exception:  # noqa: BLE001
            # fetcher 出错就当没有数据，不报警也不崩
            return None
        if not series:
            return None
        # 只取与 actual 不同期的最近 3 期均值做参考；缺则跳过
        ref_values = [v for _, v in series if v is not None and abs(v) > 1e-6]
        if len(ref_values) < 2:
            return None
        ref_mean = sum(ref_values[-3:]) / len(ref_values[-3:])
        if ref_mean == 0:
            return None
        ratio = abs(actual_f) / abs(ref_mean) if abs(ref_mean) > 0 else float("inf")
        # 偏差正反两个方向都算（actual 比 ref 大 5x 或小 5x 都标）
        if ratio >= self.magnitude_ratio or ratio <= 1 / self.magnitude_ratio:
            return (
                f"actual={actual_f:.4g} 与同公司 {metric} 近年参考均值 "
                f"{ref_mean:.4g} 的量级差 {max(ratio, 1/ratio):.1f}x"
            )
        return None


class MetricSeriesFetcher:
    """fetch(ticker, metric_canonical) -> list[(fiscal_period, value_in_yuan)]。

    具体实现由调用方提供（见 builder.py 用 SQLite 实现），这里只是接口。
    """

    def fetch(self, ticker: str, metric_canonical: str) -> list[tuple[str, float]]:
        raise NotImplementedError
