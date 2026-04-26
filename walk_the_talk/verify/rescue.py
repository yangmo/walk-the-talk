"""Rescue gate + ceiling for the verifier agent (P4 优化).

设计动机
--------
真实跑 SMIC 数据时观察到 6/22 条 claim 落进 NOT_VERIFIABLE，其中至少 4 条本可救：
LLM 第一次 query_chunks 返回 [] 就放弃，没有改写检索词重试。

本模块封装两件事，让 ``verify.agent`` 状态机保持简洁：

1. **rescue gate** —— 在 ``finalize_node`` 返回 NOT_VERIFIABLE 之前，如果还有
   工具调用预算且尚未做过 chunk 重试，就强制回到 ``plan_node`` 走一轮新的检索；
   plan 节点会拿到 ``RESCUE_RETRY_MESSAGE`` 作为引导，提示用同义词扩展 / 拓宽
   fiscal_periods / 提高 top_k。
2. **rescue ceiling** —— 救援轮触发过的 claim，即使 LLM 最终给出 VERIFIED，
   也强制下调为 PARTIALLY_VERIFIED。设计理由：rescue 路径下 LLM 是被"催"出来
   的判定，置信度天然受影响；宁愿低估也不要把不稳的证据放上 VERIFIED 高位。

使用
----
    from .rescue import RESCUE_RETRY_MESSAGE, enforce_rescue_ceiling, gate_finalize

    # finalize_node 内：
    if not forced and gate_finalize(state, obj) == "retry":
        # 回到 plan_node，下一轮 plan 会带 RESCUE_RETRY_MESSAGE
        ...

    # 落 record 之前：
    if state.get("chunk_retry_done"):
        record = enforce_rescue_ceiling(record)
"""

from __future__ import annotations

from typing import Any

from ..core.enums import Verdict
from ..core.models import VerificationRecord

__all__ = [
    "RESCUE_RETRY_MESSAGE",
    "enforce_rescue_ceiling",
    "gate_finalize",
]


# rescue 触发时塞给 plan 节点的引导文本。
# 在 plan prompt 末尾以"# 强制重试提示"段落形式注入。
RESCUE_RETRY_MESSAGE = (
    "上一轮 finalize 判定为 not_verifiable，但你仍有工具调用预算。\n"
    "在直接收尾前，请先调一次 query_chunks（或 query_financials）变体再试：\n"
    "  - 把检索词换成同义/近义表述（例：「全线紧缺」→「产能紧张 OR 供不应求」）；\n"
    "  - 拓宽 fiscal_periods 或调整 after_fiscal_year，看后续年份的解释；\n"
    "  - 提高 top_k（例：3→5）。\n"
    "完成新工具调用后再判定。判断时按「边界判定规则」严格归档：\n"
    "  - 若新证据足以支撑「方向兑现 + 后续年报原文连续印证」，请直接给 partially_verified；\n"
    "  - 若同时拿到定量印证（数据呈同向 / 满足比较），可以给 verified——\n"
    "    系统会按设计自动应用 rescue 上限，把 verified 调整为 partially_verified；\n"
    "    你不要为了避免被下调而主动收紧到 not_verifiable，那会丢掉真实证据。\n"
    "  - 仅当两种变体后**仍无任何后续印证**才回到 not_verifiable。"
)


def gate_finalize(state: dict[str, Any], obj: dict[str, Any]) -> str:
    """决定 finalize 输出是直接收尾还是回到 plan 走 rescue。

    触发 rescue 需要同时满足以下三条：

    1. 本 claim 还没做过 chunk 重试（``state["chunk_retry_done"]`` 为 False）
    2. finalize 输出 verdict 是 ``not_verifiable``（最容易被检索 miss 低估的类别）
    3. 仍有工具调用预算（``iter_count < max_iters``）

    Args:
        state: agent 状态 dict（实际是 ``_State`` TypedDict，这里取最小子集）。
            读取的 key：``chunk_retry_done`` / ``iter_count`` / ``max_iters``。
        obj: 本轮 finalize 节点的 LLM 输出 dict。读取 ``verdict``。

    Returns:
        ``"retry"``  — 应回到 plan 节点重跑一轮（caller 负责设置 retry message）
        ``"finalize"`` — 应直接落库
    """
    if state.get("chunk_retry_done", False):
        return "finalize"
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict != Verdict.NOT_VERIFIABLE.value:
        return "finalize"
    if int(state.get("iter_count", 0)) >= int(state.get("max_iters", 0)):
        return "finalize"
    return "retry"


def enforce_rescue_ceiling(record: VerificationRecord) -> VerificationRecord:
    """rescue 上限：被回锅过的 claim 即使 LLM 给 VERIFIED 也强制为 PARTIALLY_VERIFIED。

    Caller 应在判断到 ``state["chunk_retry_done"]`` 为 True 之后再调用此函数。
    非 VERIFIED 的 record 原样返回，便于无脑链式调用。

    Returns:
        - VERIFIED → 下调为 PARTIALLY_VERIFIED，并在 comment 末尾追加标注；
          ``comment`` 截断到 1000 字。
        - 其他 verdict → 原样返回。
    """
    if record.verdict != Verdict.VERIFIED:
        return record
    note = "[rescue 上限：原 verdict=verified，因证据来自重试轮，下调为 partially_verified]"
    new_comment = (record.comment + " " + note).strip() if record.comment else note
    return record.model_copy(
        update={
            "verdict": Verdict.PARTIALLY_VERIFIED,
            "comment": new_comment[:1000],
        }
    )
