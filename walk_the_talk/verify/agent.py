"""Phase 3 verifier agent：LangGraph 状态机驱动单条 claim 的验证。

状态机：
    START → plan → (call_tool | finalize)
    call_tool → (plan | finalize_forced)
    finalize → END
    finalize_forced → END

设计要点：
- LLM 解析失败一律走两层兜底：先剥 ```json fences，再 reasoner 模型重试一次。
  仍失败时 plan 默认 finalize、finalize 默认 not_verifiable，agent 永不卡死。
- ticker 由 agent 注入工具 args（query_financials），LLM 不应自己写 ticker。
- compute / query_financials / query_chunks 三类工具走 _dispatch_tool 集中调度，
  抛错或参数缺失统一以 dict 形式塞进 history（"error" 字段）。
- 缓存命中数计在 LLMResponse.cached 上，agent 只透传给 pipeline。

主入口：
    run_agent(claim, *, llm, financials_store, reports_store, current_fiscal_year,
              chat_model, reasoner_model, max_iters, ticker)
        -> AgentResult(record, history, stats)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, TypedDict

try:
    from langgraph.graph import END, START, StateGraph
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "需要 langgraph 才能跑 verify；pip install langgraph"
    ) from e

from ..core.enums import Verdict
from ..core.models import Evidence, ToolCall, VerificationRecord
from ..core.models import Claim
from ..llm import LLMClient
from ..ingest.financials_store import FinancialsStore
from .prompts import build_finalize_messages, build_plan_messages
from .tools import (
    ChunkSearcher,
    compute,
    query_chunks,
    query_financials,
)

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)

# Plan 默认动作：解析失败时直接 finalize（避免死循环）
_DEFAULT_PLAN: dict[str, Any] = {
    "action": "finalize",
    "rationale": "[fallback] plan 解析失败，直接收尾",
}

_DEFAULT_FINALIZE: dict[str, Any] = {
    "verdict": "not_verifiable",
    "actual_value": None,
    "confidence": 0.0,
    "comment": "[fallback] finalize 解析失败，回落到 not_verifiable",
    "evidence_chunk_ids": [],
}

_VALID_VERDICTS = {
    Verdict.VERIFIED.value,
    Verdict.PARTIALLY_VERIFIED.value,
    Verdict.FAILED.value,
    Verdict.NOT_VERIFIABLE.value,
    Verdict.EXPIRED.value,
}

_KNOWN_TOOLS = {"compute", "query_financials", "query_chunks"}

# rescue 触发时塞给 plan 节点的引导文本
_RESCUE_RETRY_MESSAGE = (
    "上一轮 finalize 判定为 not_verifiable，但你仍有工具调用预算。\n"
    "在直接收尾前，请先调一次 query_chunks（或 query_financials）变体再试：\n"
    "  - 把检索词换成同义/近义表述（例：「全线紧缺」→「产能紧张 OR 供不应求」）；\n"
    "  - 拓宽 fiscal_periods 或调整 after_fiscal_year，看后续年份的解释；\n"
    "  - 提高 top_k（例：3→5）。\n"
    "完成新工具调用后再判定。如新证据仍不足，再回到 not_verifiable。"
)


# ============== 出参 ==============


@dataclass
class AgentStats:
    """单 claim 跑完后的统计，给 pipeline 汇总。"""

    iter_count: int = 0
    forced_finalize: bool = False
    chat_calls: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class AgentResult:
    record: VerificationRecord
    history: list[dict[str, Any]] = field(default_factory=list)
    stats: AgentStats = field(default_factory=AgentStats)


# ============== 内部 state ==============


class _State(TypedDict, total=False):
    # 持久输入
    claim: Claim
    current_fy: int
    max_iters: int
    ticker: str

    # 演化字段
    iter_count: int
    history: list[dict[str, Any]]
    pending_tool: dict[str, Any] | None     # plan 决策出来要调的下一个工具
    next_action: str                         # "tool" | "finalize" | "finalize_forced" | "plan_retry" | "end"
    finalize_obj: dict[str, Any] | None
    final_record: VerificationRecord | None

    # rescue 机制（P4）：finalize 出 not_verifiable 时若仍有预算且尚未做过 chunk 重试，
    # 强制再走一轮 plan，提示 LLM 改写检索。retried 后即使最终判到 verified 也下调为 partially_verified。
    chunk_retry_done: bool
    force_retry_message: str | None

    # 统计
    stats: AgentStats


# ============== 主入口 ==============


def run_agent(
    claim: Claim,
    *,
    llm: LLMClient,
    financials_store: FinancialsStore,
    reports_store: ChunkSearcher | None,
    current_fiscal_year: int,
    chat_model: str,
    reasoner_model: str,
    max_iters: int,
    ticker: str,
    available_canonicals: list[str] | None = None,
) -> AgentResult:
    """对单条 claim 跑 verifier agent，返回最终 VerificationRecord 及调用历史。

    reports_store=None 时 query_chunks 会被拒绝（返回 error dict）。
    available_canonicals：financials.db 里实际存在的 line_item_canonical 白名单；
        会被注入 system prompt，让 LLM 不去问不存在的字段。None = 不注入（兼容旧调用）。
    """

    graph = _build_graph(
        llm=llm,
        financials_store=financials_store,
        reports_store=reports_store,
        chat_model=chat_model,
        reasoner_model=reasoner_model,
        ticker=ticker,
        available_canonicals=available_canonicals,
    )

    init: _State = {
        "claim": claim,
        "current_fy": current_fiscal_year,
        "max_iters": max_iters,
        "ticker": ticker,
        "iter_count": 0,
        "history": [],
        "pending_tool": None,
        "next_action": "",
        "finalize_obj": None,
        "final_record": None,
        "chunk_retry_done": False,
        "force_retry_message": None,
        "stats": AgentStats(),
    }

    final_state: _State = graph.invoke(init)  # type: ignore[arg-type]

    record = final_state.get("final_record")
    if record is None:
        # 理论上不会发生：finalize 节点总会写 final_record。这里兜个底。
        record = _build_record(
            claim,
            current_fiscal_year=current_fiscal_year,
            obj=_DEFAULT_FINALIZE,
            history=final_state.get("history", []),
            stats=final_state.get("stats", AgentStats()),
        )

    return AgentResult(
        record=record,
        history=list(final_state.get("history", [])),
        stats=final_state.get("stats", AgentStats()),
    )


# ============== StateGraph 构建 ==============


def _build_graph(
    *,
    llm: LLMClient,
    financials_store: FinancialsStore,
    reports_store: ChunkSearcher | None,
    chat_model: str,
    reasoner_model: str,
    ticker: str,
    available_canonicals: list[str] | None = None,
) -> Any:
    """编译 StateGraph。所有节点都是闭包，捕获外部依赖。"""

    g: StateGraph = StateGraph(_State)

    def plan_node(state: _State) -> dict[str, Any]:
        claim = state["claim"]
        history = state.get("history", [])
        iter_idx = state.get("iter_count", 0) + 1
        max_iters = state.get("max_iters", 4)

        force_msg = state.get("force_retry_message")
        messages = build_plan_messages(
            claim,
            current_fiscal_year=state["current_fy"],
            history=history,
            iter_index=iter_idx,
            max_iters=max_iters,
            available_canonicals=available_canonicals,
            force_retry_message=force_msg,
        )
        obj, llm_stats = _llm_json(
            llm,
            messages,
            chat_model=chat_model,
            reasoner_model=reasoner_model,
            cache_extras={
                "phase": "verify",
                "claim_id": claim.claim_id,
                "step": "plan",
                "iter": iter_idx,
                "rescue": bool(force_msg),
                "version": "v1",
            },
            default=_DEFAULT_PLAN,
        )
        stats = state["stats"]
        _accumulate(stats, llm_stats)

        action = str(obj.get("action", "finalize")).lower()
        if action == "tool":
            return {
                "iter_count": iter_idx,
                "next_action": "tool",
                "pending_tool": obj,
                # 已被消费，置空，避免下一轮 plan 重复注入
                "force_retry_message": None,
                "stats": stats,
            }
        return {
            "iter_count": iter_idx,
            "next_action": "finalize",
            "pending_tool": None,
            "force_retry_message": None,
            "stats": stats,
        }

    def call_tool_node(state: _State) -> dict[str, Any]:
        pending = state.get("pending_tool") or {}
        tool_name = str(pending.get("tool_name", "")).strip()
        args = pending.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        result = _dispatch_tool(
            tool_name,
            args,
            ticker=state["ticker"],
            financials_store=financials_store,
            reports_store=reports_store,
        )
        history = list(state.get("history", []))
        history.append(
            {
                "tool": tool_name,
                "args": args,
                "result": result,
                "rationale": pending.get("rationale", ""),
            }
        )
        # 决定下一步：还有配额则回 plan，没了就强制 finalize。
        # 注意：iter_count 在 plan 节点已经 +1，所以这里直接比较 >=。
        if state.get("iter_count", 0) >= state.get("max_iters", 4):
            return {"history": history, "next_action": "finalize_forced"}
        return {"history": history, "next_action": "plan"}

    def finalize_node(state: _State) -> dict[str, Any]:
        return _do_finalize(state, forced=False)

    def finalize_forced_node(state: _State) -> dict[str, Any]:
        return _do_finalize(state, forced=True)

    def _do_finalize(state: _State, *, forced: bool) -> dict[str, Any]:
        claim = state["claim"]
        history = state.get("history", [])
        stats = state["stats"]
        if forced:
            stats.forced_finalize = True
        # iter_count 在 plan 节点被推进；finalize 时统一同步到 stats。
        stats.iter_count = int(state.get("iter_count", 0))

        messages = build_finalize_messages(
            claim,
            current_fiscal_year=state["current_fy"],
            history=history,
            forced=forced,
            available_canonicals=available_canonicals,
        )
        obj, llm_stats = _llm_json(
            llm,
            messages,
            chat_model=chat_model,
            reasoner_model=reasoner_model,
            cache_extras={
                "phase": "verify",
                "claim_id": claim.claim_id,
                "step": "finalize",
                "forced": forced,
                "version": "v1",
            },
            default=_DEFAULT_FINALIZE,
        )
        _accumulate(stats, llm_stats)

        # ---- rescue gate（仅非强制 finalize 才有机会触发）----
        if not forced and _gate_finalize(state, obj) == "retry":
            log.info(
                "[rescue] claim=%s verdict=%s iter=%d/%d → 强制再走一轮 plan",
                claim.claim_id,
                obj.get("verdict"),
                stats.iter_count,
                state.get("max_iters", 0),
            )
            return {
                "next_action": "plan_retry",
                "chunk_retry_done": True,
                "force_retry_message": _RESCUE_RETRY_MESSAGE,
                # 不写 final_record / finalize_obj，让 plan 重新跑
                "stats": stats,
            }

        record = _build_record(
            claim,
            current_fiscal_year=state["current_fy"],
            obj=obj,
            history=history,
            stats=stats,
        )
        # rescue 上限：retried 过的，verified 一律下调为 partially_verified
        if state.get("chunk_retry_done", False):
            record = _enforce_rescue_ceiling(record)

        return {
            "final_record": record,
            "finalize_obj": obj,
            "next_action": "end",
            "stats": stats,
        }

    # 注册节点
    g.add_node("plan", plan_node)
    g.add_node("call_tool", call_tool_node)
    g.add_node("finalize", finalize_node)
    g.add_node("finalize_forced", finalize_forced_node)

    # 边
    g.add_edge(START, "plan")
    g.add_conditional_edges(
        "plan",
        lambda s: s.get("next_action", "finalize"),
        {"tool": "call_tool", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "call_tool",
        lambda s: s.get("next_action", "plan"),
        {"plan": "plan", "finalize_forced": "finalize_forced"},
    )
    # finalize 节点出边新增 plan_retry：rescue 触发时回到 plan 重跑一轮
    g.add_conditional_edges(
        "finalize",
        lambda s: s.get("next_action", "end"),
        {"plan_retry": "plan", "end": END},
    )
    g.add_edge("finalize_forced", END)

    return g.compile()


# ============== LLM 调用 + JSON 解析 ==============


def _llm_json(
    llm: LLMClient,
    messages: list[dict[str, str]],
    *,
    chat_model: str,
    reasoner_model: str,
    cache_extras: dict[str, Any],
    default: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    """两层兜底地从 LLM 拿一个 JSON 对象。

    - 一层：deepseek-chat + response_format=json_object，json.loads。
    - 二层：deepseek-reasoner（不强制 json_object），先剥 ```json fences。
    - 全失败：返回 default。

    返回 (parsed_obj, stats_dict)。stats_dict 字段：
        chat_calls, cache_hits, prompt_tokens, completion_tokens, total_tokens
    """
    stats = {
        "chat_calls": 0,
        "cache_hits": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    # ---- 第一层 ----
    obj, err = _try_chat(
        llm,
        messages,
        model=chat_model,
        json_mode=True,
        cache_extras=cache_extras,
        stats=stats,
    )
    if obj is not None:
        return obj, stats
    log.warning("verifier plan/finalize chat 失败：%s；降级 reasoner", err)

    # ---- 第二层 ----
    obj, err = _try_chat(
        llm,
        messages,
        model=reasoner_model,
        json_mode=False,
        cache_extras={**cache_extras, "fallback": "reasoner"},
        stats=stats,
    )
    if obj is not None:
        return obj, stats
    log.error("verifier reasoner 也失败：%s；用 default 回落", err)
    return dict(default), stats


def _try_chat(
    llm: LLMClient,
    messages: list[dict[str, str]],
    *,
    model: str,
    json_mode: bool,
    cache_extras: dict[str, Any],
    stats: dict[str, int],
) -> tuple[dict[str, Any] | None, str | None]:
    """单次 LLM 调用 + JSON 解析。"""
    response_format: dict[str, Any] | None = (
        {"type": "json_object"} if json_mode else None
    )
    # cache_extras 由 chat 内部并入 cache key（若 client 支持），
    # 但当前 DeepSeekClient 的 cache key 只看 model+messages+(temperature/max_tokens/response_format)。
    # 这里把 cache_extras 注入到一个 dummy assistant-style 字段会污染 prompt，
    # 因此本步骤暂不依赖 cache_extras 影响 cache key —— 让相同 messages 自然命中已有 cache。
    # cache_extras 仅用于日志 / 调试。
    _ = cache_extras

    try:
        resp = llm.chat(
            messages,
            model=model,
            temperature=0.0,
            response_format=response_format,
            timeout=120.0,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"

    stats["chat_calls"] += 1
    if getattr(resp, "cached", False):
        stats["cache_hits"] += 1
    stats["prompt_tokens"] += int(getattr(resp, "prompt_tokens", 0) or 0)
    stats["completion_tokens"] += int(getattr(resp, "completion_tokens", 0) or 0)
    stats["total_tokens"] += int(getattr(resp, "total_tokens", 0) or 0)

    text = (resp.text or "").strip()
    if not text:
        return None, "empty response"

    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as je:
        return None, f"JSONDecodeError: {je.msg} @ pos {je.pos}"

    if not isinstance(obj, dict):
        return None, f"top-level not a JSON object (got {type(obj).__name__})"
    return obj, None


# ============== 工具调度 ==============


def _dispatch_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    ticker: str,
    financials_store: FinancialsStore,
    reports_store: ChunkSearcher | None,
) -> Any:
    """执行 plan 决定的工具调用，所有异常包装成 {"error": ...} dict。"""
    if tool_name not in _KNOWN_TOOLS:
        return {"error": f"unknown tool: {tool_name!r}; allowed: {sorted(_KNOWN_TOOLS)}"}

    try:
        if tool_name == "compute":
            expr = str(args.get("expr", "")).strip()
            return compute(expr)

        if tool_name == "query_financials":
            line_item = str(args.get("line_item_canonical", "")).strip()
            if not line_item:
                return {"error": "missing arg: line_item_canonical"}
            fps_raw = args.get("fiscal_periods")
            fps: list[str] | None
            if fps_raw is None:
                fps = None
            elif isinstance(fps_raw, list):
                fps = [str(x) for x in fps_raw]
            else:
                return {"error": "fiscal_periods must be a list or null"}
            return query_financials(
                financials_store,
                ticker=ticker,
                line_item_canonical=line_item,
                fiscal_periods=fps,
            )

        if tool_name == "query_chunks":
            if reports_store is None:
                return {"error": "reports_store 未配置（仅 financials 验证模式）"}
            qry = str(args.get("query", "")).strip()
            if not qry:
                return {"error": "missing arg: query"}
            after = args.get("after_fiscal_year")
            after_int = int(after) if isinstance(after, (int, float)) else None
            fps_raw = args.get("fiscal_periods")
            fps_typed: list[str] | None
            if fps_raw is None:
                fps_typed = None
            elif isinstance(fps_raw, list):
                fps_typed = [str(x) for x in fps_raw]
            else:
                return {"error": "fiscal_periods must be a list or null"}
            top_k = int(args.get("top_k", 3) or 3)
            return query_chunks(
                reports_store,
                query=qry,
                after_fiscal_year=after_int,
                fiscal_periods=fps_typed,
                top_k=top_k,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s 抛错", tool_name)
        return {"error": f"{type(e).__name__}: {e}"}

    # 兜底（理论不可达）
    return {"error": f"tool {tool_name} not implemented"}


# ============== finalize → VerificationRecord ==============


def _build_record(
    claim: Claim,
    *,
    current_fiscal_year: int,
    obj: dict[str, Any],
    history: list[dict[str, Any]],
    stats: AgentStats,
) -> VerificationRecord:
    """LLM finalize 输出 → VerificationRecord（带兜底）。"""
    raw_verdict = str(obj.get("verdict", Verdict.NOT_VERIFIABLE.value)).strip().lower()
    if raw_verdict not in _VALID_VERDICTS:
        log.warning("finalize 返回非法 verdict %r，回落 not_verifiable", raw_verdict)
        raw_verdict = Verdict.NOT_VERIFIABLE.value
    verdict = Verdict(raw_verdict)

    confidence_raw = obj.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    actual_value = obj.get("actual_value")
    comment = str(obj.get("comment", "") or "")[:1000]

    # computation_trace
    trace = [
        ToolCall(
            tool_name=str(h.get("tool", "")),
            args=dict(h.get("args", {}) or {}),
            result=h.get("result"),
            error=_extract_error(h.get("result")),
        )
        for h in history
    ]

    # evidence：从 query_chunks 历史里挑 LLM 引用的 chunk_id
    referenced_ids = obj.get("evidence_chunk_ids") or []
    if not isinstance(referenced_ids, list):
        referenced_ids = []
    evidence = _collect_evidence(history, referenced_ids)

    cost = {
        "prompt_tokens": stats.prompt_tokens,
        "completion_tokens": stats.completion_tokens,
        "total_tokens": stats.total_tokens,
        "cache_hits": stats.cache_hits,
        "chat_calls": stats.chat_calls,
        "iter_count": stats.iter_count,
        "forced_finalize": stats.forced_finalize,
    }

    return VerificationRecord(
        fiscal_year=current_fiscal_year,
        verdict=verdict,
        target_value=claim.predicate.value,
        actual_value=actual_value,
        evidence=evidence,
        computation_trace=trace,
        confidence=confidence,
        comment=comment,
        cost=cost,
    )


def _extract_error(result: Any) -> str | None:
    if isinstance(result, dict):
        err = result.get("error")
        return str(err) if err else None
    return None


def _gate_finalize(state: _State, obj: dict[str, Any]) -> str:
    """决定 finalize 出来的 obj 是直接收尾还是回到 plan 走 rescue。

    触发 rescue 的全部条件（AND）：
    - 还没做过 chunk 重试（chunk_retry_done=False）
    - finalize verdict 是 not_verifiable（最容易因检索 miss 被低估的类别）
    - 仍有 iter 预算（iter_count < max_iters）

    返回 "retry" 或 "finalize"。
    """
    if state.get("chunk_retry_done", False):
        return "finalize"
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict != Verdict.NOT_VERIFIABLE.value:
        return "finalize"
    if int(state.get("iter_count", 0)) >= int(state.get("max_iters", 0)):
        return "finalize"
    return "retry"


def _enforce_rescue_ceiling(record: VerificationRecord) -> VerificationRecord:
    """rescue 上限：被回锅过的 claim 即使 LLM 给了 verified 也只能算 partially_verified。

    设计理由：rescue 路径下 LLM 是被"催"出来的判定，置信度天然受影响；
    我们宁愿低估也不要把不稳的证据放上 verified 高位。
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


def _collect_evidence(
    history: list[dict[str, Any]], referenced_ids: list[Any]
) -> list[Evidence]:
    """从 query_chunks 历史里抽出被 LLM 在 finalize 引用的 chunk 作为 Evidence。"""
    if not referenced_ids:
        return []
    wanted = {str(x) for x in referenced_ids if x}
    seen: set[str] = set()
    out: list[Evidence] = []
    for h in history:
        if h.get("tool") != "query_chunks":
            continue
        result = h.get("result")
        if not isinstance(result, list):
            continue
        for item in result:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("chunk_id", ""))
            if cid not in wanted or cid in seen:
                continue
            seen.add(cid)
            out.append(
                Evidence(
                    quote=str(item.get("text", ""))[:500],
                    locator=str(item.get("locator", "")),
                    source_path=str(item.get("source_path", "")),
                )
            )
    return out


def _accumulate(target: AgentStats, src: dict[str, int]) -> None:
    target.chat_calls += int(src.get("chat_calls", 0))
    target.cache_hits += int(src.get("cache_hits", 0))
    target.prompt_tokens += int(src.get("prompt_tokens", 0))
    target.completion_tokens += int(src.get("completion_tokens", 0))
    target.total_tokens += int(src.get("total_tokens", 0))


__all__ = [
    "AgentResult",
    "AgentStats",
    "run_agent",
]
