"""Phase 3 verifier agent 的提示词（plan / finalize 两节点）。

设计要点：
- 两类节点共用同一个 system prompt（包含工具说明 + verdict 定义），减少漂移。
- plan 节点的输出严格固定为 {"action": "tool"|"finalize", ...}，便于状态机分发。
- finalize 节点输出固定为 {"verdict": ..., "actual_value": ..., "confidence": ..., "comment": ...}，
  与 VerificationRecord 字段一一对应。
- 全程使用 response_format=json_object（DeepSeek-chat 支持），减少 JSON 损坏。
- 历史 tool_history 用紧凑文本拼接，避免 context 爆炸；agent.py 负责裁剪超长 tool result。

调用约定：
    build_plan_messages(claim, current_fy, history)        -> messages
    build_finalize_messages(claim, current_fy, history)    -> messages

history 是 list[dict]，每个 dict：{tool, args, result_summary}。
"""

from __future__ import annotations

import json
from typing import Any

from ..core.models import Claim

# ============== 通用 system prompt ==============

# 注意：PREMATURE 已经被 pipeline 在 agent 之前短路掉，所以 finalize 输出里不允许 premature。
_VERDICT_DEFS = """# Verdict 五选一（不允许 "premature"，那个由调用方短路）

- verified         ：claim 的目标值在事实数据上完全达成（数值满足比较，或定性目标已落地）。
- partially_verified：方向正确但程度不足，或多目标里部分达成。
- failed           ：事实数据明确否定 claim（数值未达，或承诺未落地）。
- not_verifiable   ：claim 模糊到无法用现有数据判断（措辞过软、缺定量目标、或定性范围太大）。
- expired          ：claim 的窗口已过 (horizon.end ≤ current_fy)，但所需数据 ingest 阶段没拿到。

判定原则：
1. 优先调用 query_financials 取数；若 line_item_canonical 不在库里，先用工具返回的 hint
   重试一次（如把 capex_yoy 改成 capex 自己除一下）。
2. 数值比较一律用 compute(expr) 做；不要心算。
3. 实在缺数才用 query_chunks 找原文佐证（成本高，谨慎）。
4. confidence ∈ [0, 1]，0.9 以上保留给"工具完整支撑 + 数值精确比对成功"的场景。
"""

_TOOLS_DOC_TEMPLATE = """# 可用工具

## compute(expr: str) -> {{"expr": str, "value": <num|bool> | "error": str}}
安全表达式求值；只支持算术 / 比较 / 布尔 / abs/min/max/round。
例：compute("(57796 - 45525) / 45525 >= 0.30")

## query_financials(line_item_canonical: str, fiscal_periods: list[str] | null)
    -> {{"line_item": str, "values": {{fp: value}}, "unit": "元"}}
       或比率派生字段：{{"line_item": str, "values": {{fp: ratio}}, "unit": "ratio",
                        "derived": true, "description": "...", "requires": [...]}}
       或求和派生字段：{{"line_item": str, "values": {{fp: amount}}, "unit": "元",
                        "derived": true, "description": "...", "requires": [...],
                        "optional_requires": [...]}}
       或 {{"error": ..., "hint": "did you mean 'X'?", "available_canonicals": [...]}}
       或 {{"values": {{}}, "available_fiscal_periods": [...], "error": "no data for ..."}}
ticker 由 agent 自动注入，**不要在 args 里传 ticker**。
fiscal_periods=null 表示该 line_item 全部财年。

**重要 · line_item_canonical 白名单**：以下字段可直接 query_financials。其中：
- 基础字段（如 revenue, net_profit, ocf, capex, total_assets,
  depreciation, depreciation_right_of_use, amortization_intangible 等）从 financials.db 直查；
- 比率派生字段（gross_margin, net_margin, operating_margin, fcf_margin）会从基础字段实时计算，
  返回值是无量纲比率（如 0.215 表示 21.5%）；
- 求和派生字段（depreciation_amortization_total）会把所有可选依赖加总（缺失项视为 0），
  返回值单位是"元"；至少 1 项依赖有数才会给值。
派生字段的调用方式和基础字段完全一样。

不在白名单里的概念（如产能/月产量/客户数/技术节点等）数据库里**没有**，不要去尝试 query_financials，
请直接用 query_chunks 找原文，或先用 compute 从已有字段算。
{canonicals_block}

## query_chunks(query: str, after_fiscal_year: int | null, fiscal_periods: list[str] | null,
               top_k: int = 3) -> [{{chunk_id, score, fiscal_period, section, locator, text}}, ...]
混合检索找原文证据。after_fiscal_year 用于"看后续年份的解释"；fiscal_periods 显式枚举。
两者最多给一个；都不给则全库检索（不推荐）。

# query_chunks 检索失败的处理（rescue 策略）

- query_chunks 第一次返回 [] 不要立即放弃，**至少**尝试以下任一变体再查一次：
  1. 把名词改成同义/近义词（例：「全线紧缺」→「产能紧张 OR 供不应求」；「持续优化」→「改善 OR 提升 OR 完善」）
  2. 拓宽 fiscal_periods（例：[FY2022] → [FY2022, FY2023]），或改用 after_fiscal_year 看后续解释
  3. 提高 top_k 到 5（默认 3）
- 第二次仍 miss 才考虑 not_verifiable。
- 对于定性 / 方向性 claim（如「有序推进」「持续优化」「稳步提升」），即使数据没说死，
  只要后续年份原文出现了一致语调（例如年报继续提及该项目仍在进行、相关指标仍在改善）即可判
  partially_verified 或 verified，不要因为「无定量数据」就一律 not_verifiable。
"""


_SYSTEM_TEMPLATE = """你是一位严格、谨慎的中文财务断言验证专家。

# 任务

给定一条上市公司管理层在某年报里发出的"前瞻性断言"(claim)，
你需要通过调用三个原子工具收集证据，最后给出一个 Verdict。

{verdict_defs}

{tools_doc}

# 工作流

你会被反复调用：
- "plan" 阶段：决定下一步是再调一个工具，还是已经够信息可以收尾。
- "finalize" 阶段：在收集到足够证据 / 工具配额耗尽后，输出最终 Verdict。

每一轮的输出**必须是单个 JSON 对象**，不要包裹 markdown，不要解释。
"""


def _render_canonicals_block(available_canonicals: list[str] | None) -> str:
    """把白名单渲染成 system prompt 嵌入块。

    None / [] → 退化提示（仍然兼容老路径）；
    非空      → 排序 + 逗号空格拼接（一次约 1500 token，但能让 LLM 直接绕过不存在的字段）。
    """
    if not available_canonicals:
        return "（白名单未注入；fallback：先尝试常见 canonical，miss 后看 hint 重试）"
    sorted_canonicals = sorted(set(available_canonicals))
    joined = ", ".join(sorted_canonicals)
    return f"共 {len(sorted_canonicals)} 项：{joined}"


def build_system_prompt(available_canonicals: list[str] | None = None) -> str:
    """构造 system prompt，注入 financials canonical 白名单。"""
    canonicals_block = _render_canonicals_block(available_canonicals)
    tools_doc = _TOOLS_DOC_TEMPLATE.format(canonicals_block=canonicals_block)
    return _SYSTEM_TEMPLATE.format(
        verdict_defs=_VERDICT_DEFS,
        tools_doc=tools_doc,
    )


# 向后兼容：模块级常量保留，但默认不含白名单（agent 必传 available_canonicals 走 build_system_prompt）。
SYSTEM_PROMPT = build_system_prompt(None)


# ============== Plan 节点 ==============

_PLAN_SCHEMA = """# 输出 schema（plan 阶段）

二选一：

继续调工具：
{
  "action": "tool",
  "tool_name": "compute" | "query_financials" | "query_chunks",
  "args": { ... 与该工具参数对齐 ... },
  "rationale": "为什么调它（≤80 字）"
}

收尾：
{
  "action": "finalize",
  "rationale": "为什么现在可以判定（≤80 字）"
}
"""


def build_plan_messages(
    claim: Claim,
    *,
    current_fiscal_year: int,
    history: list[dict[str, Any]],
    iter_index: int,
    max_iters: int,
    available_canonicals: list[str] | None = None,
    force_retry_message: str | None = None,
) -> list[dict[str, str]]:
    """plan 节点的 messages：system + user(claim+history+meta)。

    available_canonicals=None 退化为不带白名单的 system prompt（兼容旧测试路径）。
    force_retry_message：rescue 流程触发时由 agent 注入的引导文本，提示 LLM
    本轮被强制回到 plan 阶段，必须先尝试一次新的检索变体（不能立即 finalize）。
    """

    retry_block = f"# 强制重试提示\n{force_retry_message}\n\n" if force_retry_message else ""
    user_block = (
        f"{retry_block}"
        f"current_fiscal_year: FY{current_fiscal_year}\n"
        f"iter: {iter_index} / {max_iters}\n\n"
        f"# Claim\n{_format_claim(claim)}\n\n"
        f"# 已调过的工具（按时间序）\n{_format_history(history)}\n\n"
        f"{_PLAN_SCHEMA}"
    )
    return [
        {"role": "system", "content": build_system_prompt(available_canonicals)},
        {"role": "user", "content": user_block},
    ]


# ============== Finalize 节点 ==============

_FINALIZE_SCHEMA = """# 输出 schema（finalize 阶段）

{
  "verdict": "verified" | "partially_verified" | "failed" | "not_verifiable" | "expired",
  "actual_value": <数字 | 字符串 | null>,
  "confidence": <0~1 的小数>,
  "comment": "≤200 字，说明判定依据；引用 tool 结果时给数字。",
  "evidence_chunk_ids": ["chunk_id1", "chunk_id2"]   // 来自 query_chunks 的引用，可空数组
}
"""


def build_finalize_messages(
    claim: Claim,
    *,
    current_fiscal_year: int,
    history: list[dict[str, Any]],
    forced: bool,
    available_canonicals: list[str] | None = None,
) -> list[dict[str, str]]:
    """finalize 节点的 messages。

    forced=True 表示是因为 max_iters 耗尽被强制收尾；提示 LLM 用现有信息做最佳判断。
    available_canonicals=None 退化为不带白名单的 system prompt。
    """
    forced_note = (
        "**注意**：你已用尽工具配额，现在必须基于已有信息给出最佳判断；如证据不足，使用 not_verifiable。\n\n"
        if forced
        else ""
    )
    user_block = (
        f"{forced_note}"
        f"current_fiscal_year: FY{current_fiscal_year}\n\n"
        f"# Claim\n{_format_claim(claim)}\n\n"
        f"# 已收集到的工具调用历史\n{_format_history(history)}\n\n"
        f"{_FINALIZE_SCHEMA}"
    )
    return [
        {"role": "system", "content": build_system_prompt(available_canonicals)},
        {"role": "user", "content": user_block},
    ]


# ============== Helpers ==============

# tool result 文本截断，防止 LLM context 爆炸
_RESULT_PREVIEW_CHARS = 800


def _format_claim(claim: Claim) -> str:
    """把 Claim 序列化成 plain-text 块，给 LLM 看。"""
    pred = claim.predicate
    hor = claim.horizon
    plan = claim.verification_plan
    return (
        f"- claim_id: {claim.claim_id}\n"
        f"- claim_type: {claim.claim_type}\n"
        f"- from_fiscal_year: FY{claim.from_fiscal_year}\n"
        f"- speaker: {claim.speaker}\n"
        f"- original_text: {claim.original_text}\n"
        f"- subject: scope={claim.subject.scope}, name={claim.subject.name!r}\n"
        f"- metric: {claim.metric!r} (canonical={claim.metric_canonical!r})\n"
        f"- predicate: operator={pred.operator!r} value={pred.value!r} unit={pred.unit!r}\n"
        f"- horizon: type={hor.type!r} start={hor.start!r} end={hor.end!r}\n"
        f"- conditions: {claim.conditions!r}\n"
        f"- hedging_words: {claim.hedging_words}\n"
        f"- verification_plan: required_line_items={plan.required_line_items} "
        f"computation={plan.computation!r} comparison={plan.comparison!r}"
    )


def _format_history(history: list[dict[str, Any]]) -> str:
    """工具历史 → 文本块。

    每条 history 形如：{"tool": "compute", "args": {...}, "result": <any>}。
    """
    if not history:
        return "(空)"
    parts: list[str] = []
    for i, h in enumerate(history, 1):
        result_text = json.dumps(h.get("result"), ensure_ascii=False, default=str)
        if len(result_text) > _RESULT_PREVIEW_CHARS:
            result_text = result_text[:_RESULT_PREVIEW_CHARS] + "…(truncated)"
        args_text = json.dumps(h.get("args", {}), ensure_ascii=False, default=str)
        parts.append(f"[{i}] {h.get('tool', '?')}({args_text}) → {result_text}")
    return "\n".join(parts)


__all__ = [
    "SYSTEM_PROMPT",
    "build_finalize_messages",
    "build_plan_messages",
    "build_system_prompt",
]
