"""Phase 3 verify agent tests：LangGraph 状态机 + 工具调度，用 stub LLM 走遍所有分支。

覆盖：
1. 5 类 verdict 落地：verified / partially_verified / failed / not_verifiable / expired
2. 工具调度：compute / query_financials / query_chunks 三类正确分发
3. max_iters 强制 finalize 路径（forced_finalize=True）
4. plan/finalize JSON 解析失败 → reasoner 兜底 → default 兜底
5. 未知工具名 / 缺参数 → tool result 写 error，agent 继续
6. evidence_chunk_ids 引用回填 evidence
7. ticker 自动注入 query_financials（LLM 不传）
8. query_chunks 在 reports_store=None 时返回 error
9. computation_trace 完整记录每一步
10. cost dict 正确累计

不打真实网络；不依赖 langchain 之外的 LLM SDK。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from walk_the_talk.core.enums import (
    ClaimType,
    SectionCanonical,
    StatementType,
    Verdict,
)
from walk_the_talk.core.models import (
    Claim,
    FinancialLine,
    Horizon,
    Predicate,
    Subject,
    VerificationPlan,
)
from walk_the_talk.ingest.financials_store import FinancialsStore
from walk_the_talk.llm import LLMClient, LLMResponse
from walk_the_talk.verify.agent import _KNOWN_TOOLS, AgentResult, run_agent


# ============== 通用 helpers ==============


def _make_claim(
    *,
    claim_id: str = "000001-FY2024-001",
    from_fy: int = 2024,
    metric_canonical: str = "revenue",
    end_fy: str = "FY2025",
) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=ClaimType.QUANTITATIVE_FORECAST,
        section="管理层讨论与分析",
        section_canonical=SectionCanonical.MDA,
        original_text="预计 2025 年收入同比增长不低于 10%",
        locator=f"{claim_id}#1",
        subject=Subject(scope="整体"),
        metric="营业收入同比增长率",
        metric_canonical=metric_canonical,
        predicate=Predicate(operator=">=", value=0.10, unit="%"),
        horizon=Horizon(type="财年", start="FY2025", end=end_fy),
        from_fiscal_year=from_fy,
        canonical_key=f"{metric_canonical}|整体|FY2025~{end_fy}",
        verification_plan=VerificationPlan(
            required_line_items=["revenue"],
            computation="(revenue_FY2025 - revenue_FY2024) / revenue_FY2024",
            comparison=">= 0.10",
        ),
    )


def _seed_financials(tmp_path: Path) -> FinancialsStore:
    """Seed FY2024=1.0e9 / FY2025=1.2e9 revenue 让 compute 能算 +20%。"""
    db = tmp_path / "fin.db"
    fs = FinancialsStore(db)
    fs.upsert_lines(
        [
            FinancialLine(
                ticker="000001",
                fiscal_period="FY2024",
                statement_type=StatementType.INCOME,
                line_item="营业收入",
                line_item_canonical="revenue",
                value=1.0e9,
            ),
            FinancialLine(
                ticker="000001",
                fiscal_period="FY2025",
                statement_type=StatementType.INCOME,
                line_item="营业收入",
                line_item_canonical="revenue",
                value=1.2e9,
            ),
        ]
    )
    return fs


@dataclass
class _StubReports:
    """简易 ChunkSearcher：返回固定 hits + 固定 texts。"""

    hits: list[tuple[str, float, dict]]
    texts: dict[str, str]

    def query_hybrid(self, text, k=10, where=None, alpha=0.5):
        return list(self.hits)

    def get_texts(self, ids):
        return {i: self.texts.get(i, "") for i in ids}


# ============== StubLLM：脚本化 plan/finalize 响应 ==============


class _StubLLM(LLMClient):
    """脚本化 LLM：按调用次数依次返回预设脚本里的 (text, model)。

    用 `_classify_step(messages)` 区分当前是 plan 还是 finalize。
    脚本格式：list[dict]，每项包含：
        {"step": "plan"|"finalize", "text": "<json>", "model": "deepseek-chat"|"deepseek-reasoner",
         "raise": Exception | None}
    每次 chat() 弹出脚本里 step 匹配的下一条；不匹配抛 AssertionError 防止回归静默。
    """

    name = "stub"

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages,
        *,
        model,
        temperature=0.0,
        max_tokens=None,
        response_format=None,
        timeout=60.0,
    ):
        step = _classify_step(messages)
        # 找脚本里下一条 step 匹配的项
        for i, item in enumerate(self.script):
            if item.get("step") == step:
                hit = self.script.pop(i)
                break
        else:
            raise AssertionError(
                f"StubLLM 脚本耗尽：step={step!r}, 剩余={self.script}"
            )

        self.calls.append(
            {"step": step, "model": model, "messages_len": len(messages)}
        )

        if hit.get("raise") is not None:
            raise hit["raise"]

        return LLMResponse(
            text=hit["text"],
            model=hit.get("model", model),
            prompt_tokens=hit.get("prompt_tokens", 10),
            completion_tokens=hit.get("completion_tokens", 5),
            total_tokens=hit.get("total_tokens", 15),
            cached=hit.get("cached", False),
        )


def _classify_step(messages: list[dict[str, str]]) -> str:
    """根据最后 user 消息内容区分 plan / finalize。"""
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m.get("content", "")
            break
    if "plan 阶段" in last_user:
        return "plan"
    if "finalize 阶段" in last_user:
        return "finalize"
    return "unknown"


def _plan_tool(tool_name: str, args: dict[str, Any], rationale: str = "stub") -> dict:
    return {
        "step": "plan",
        "text": json.dumps(
            {"action": "tool", "tool_name": tool_name, "args": args, "rationale": rationale},
            ensure_ascii=False,
        ),
        "model": "deepseek-chat",
    }


def _plan_finalize(rationale: str = "stub done") -> dict:
    return {
        "step": "plan",
        "text": json.dumps(
            {"action": "finalize", "rationale": rationale}, ensure_ascii=False
        ),
        "model": "deepseek-chat",
    }


def _finalize(
    verdict: str,
    *,
    actual_value: Any = None,
    confidence: float = 0.85,
    comment: str = "stub",
    evidence_chunk_ids: list[str] | None = None,
) -> dict:
    return {
        "step": "finalize",
        "text": json.dumps(
            {
                "verdict": verdict,
                "actual_value": actual_value,
                "confidence": confidence,
                "comment": comment,
                "evidence_chunk_ids": evidence_chunk_ids or [],
            },
            ensure_ascii=False,
        ),
        "model": "deepseek-chat",
    }


def _run(
    claim: Claim,
    *,
    llm: _StubLLM,
    fs: FinancialsStore | None = None,
    reports: _StubReports | None = None,
    current_fy: int = 2025,
    max_iters: int = 3,
) -> AgentResult:
    """轻封装 run_agent；fs 可空（会用临时 db）。"""
    if fs is None:
        fs = FinancialsStore(":memory:")
    return run_agent(
        claim,
        llm=llm,
        financials_store=fs,
        reports_store=reports,
        current_fiscal_year=current_fy,
        chat_model="deepseek-chat",
        reasoner_model="deepseek-reasoner",
        max_iters=max_iters,
        ticker="000001",
    )


# ============== 1. 5 类 verdict 落地 ==============


def test_verdict_verified_via_compute(tmp_path: Path) -> None:
    """plan: query_financials → plan: compute → plan: finalize → finalize: verified。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("query_financials", {"line_item_canonical": "revenue"}),
            _plan_tool("compute", {"expr": "(1.2e9 - 1.0e9) / 1.0e9 >= 0.10"}),
            _plan_finalize("数据已齐"),
            _finalize("verified", actual_value=0.20, confidence=0.95, comment="20% >= 10%"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs)

    assert res.record.verdict == Verdict.VERIFIED
    assert res.record.actual_value == 0.20
    assert res.record.confidence == 0.95
    assert len(res.record.computation_trace) == 2
    assert res.record.computation_trace[0].tool_name == "query_financials"
    # ticker 是 agent 自动注入，不在 LLM 提交的 args 里
    assert "ticker" not in res.record.computation_trace[0].args
    # query_financials 命中
    assert res.record.computation_trace[0].result["values"] == {
        "FY2024": 1.0e9,
        "FY2025": 1.2e9,
    }
    # compute 算对
    assert res.record.computation_trace[1].result["value"] is True
    assert res.stats.chat_calls == 4
    assert res.stats.iter_count == 3
    assert res.stats.forced_finalize is False


def test_verdict_partially_verified(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("partially_verified", confidence=0.6, comment="方向对程度不足"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    assert res.record.verdict == Verdict.PARTIALLY_VERIFIED
    assert res.record.confidence == 0.6


def test_verdict_failed(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("failed", actual_value=0.05, confidence=0.9, comment="未达标"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    assert res.record.verdict == Verdict.FAILED
    assert res.record.actual_value == 0.05


def test_verdict_not_verifiable(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("not_verifiable", confidence=0.0, comment="原文太软"),
        ]
    )
    # max_iters=1：iter_count 立即耗尽，rescue 不触发（这里只测 verdict 落地）
    res = _run(claim, llm=llm, fs=fs, max_iters=1)
    assert res.record.verdict == Verdict.NOT_VERIFIABLE


def test_verdict_expired(tmp_path: Path) -> None:
    """horizon 已过但数据缺失 → expired。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim(end_fy="FY2024")  # 已过期但要看 FY2024 数据
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("expired", confidence=1.0, comment="缺 FY2024 line_item"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    assert res.record.verdict == Verdict.EXPIRED


# ============== 2. 工具调度 ==============


def test_dispatch_compute_tool(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("compute", {"expr": "abs(-5) + 3"}),
            _plan_finalize(),
            _finalize("verified", actual_value=8),
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    assert res.record.computation_trace[0].tool_name == "compute"
    assert res.record.computation_trace[0].result == {"expr": "abs(-5) + 3", "value": 8}


def test_dispatch_query_chunks(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    reports = _StubReports(
        hits=[
            ("000001-FY2025-mda-p001", 0.9, {
                "fiscal_period": "FY2025",
                "section": "管理层讨论与分析",
                "section_canonical": "mda",
                "locator": "管理层讨论与分析#1",
                "source_path": "/tmp/2025.html",
            }),
        ],
        texts={"000001-FY2025-mda-p001": "公司 2025 年收入同比增长 20%……"},
    )
    llm = _StubLLM(
        [
            _plan_tool("query_chunks", {"query": "营业收入 同比增长", "fiscal_periods": ["FY2025"]}),
            _plan_finalize(),
            _finalize(
                "verified",
                actual_value=0.20,
                confidence=0.9,
                comment="原文佐证",
                evidence_chunk_ids=["000001-FY2025-mda-p001"],
            ),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, reports=reports)
    assert res.record.computation_trace[0].tool_name == "query_chunks"
    assert len(res.record.computation_trace[0].result) == 1
    # evidence 被 finalize 引用回填
    assert len(res.record.evidence) == 1
    assert res.record.evidence[0].locator == "管理层讨论与分析#1"
    assert res.record.evidence[0].quote.startswith("公司 2025 年收入")


def test_query_chunks_without_reports_store(tmp_path: Path) -> None:
    """reports_store=None 时 query_chunks 返回 error，agent 继续。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("query_chunks", {"query": "..."}),
            _plan_finalize(),
            _finalize("not_verifiable"),
        ]
    )
    # max_iters=2：plan(1)+call_tool+plan(2)+finalize 后 iter=2>=2，rescue 不触发
    res = _run(claim, llm=llm, fs=fs, reports=None, max_iters=2)
    err = res.record.computation_trace[0].error
    assert err is not None and "reports_store" in err


def test_unknown_tool_name(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("delete_database", {"target": "/"}),
            _plan_finalize(),
            _finalize("not_verifiable"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=2)  # rescue 不触发：iter=2>=2
    err = res.record.computation_trace[0].error
    assert err is not None and "unknown tool" in err
    # 未知工具不会污染已知工具集
    assert "delete_database" not in _KNOWN_TOOLS


def test_query_financials_missing_args(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("query_financials", {}),
            _plan_finalize(),
            _finalize("not_verifiable"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=2)  # rescue 不触发
    err = res.record.computation_trace[0].error
    assert err is not None and "line_item_canonical" in err


def test_query_financials_alias_hint(tmp_path: Path) -> None:
    """LLM 写错 line_item_canonical（capex_yoy），工具回 hint=capex（如果有）。"""
    fs = _seed_financials(tmp_path)
    fs.upsert_lines(
        [
            FinancialLine(
                ticker="000001",
                fiscal_period="FY2024",
                statement_type=StatementType.CAPEX,
                line_item="资本开支",
                line_item_canonical="capex",
                value=5.0e8,
            )
        ]
    )
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("query_financials", {"line_item_canonical": "capex_yoy"}),
            _plan_tool("query_financials", {"line_item_canonical": "capex"}),
            _plan_finalize(),
            _finalize("verified", actual_value=5.0e8),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=4)
    first = res.record.computation_trace[0].result
    assert first["error"].startswith("line_item 'capex_yoy' not found")
    assert first["hint"] is not None and "capex" in first["hint"]
    second = res.record.computation_trace[1].result
    assert second["line_item"] == "capex"
    assert second["values"] == {"FY2024": 5.0e8}


# ============== 3. max_iters 强制 finalize ==============


def test_max_iters_forces_finalize(tmp_path: Path) -> None:
    """LLM 一直要工具，max_iters=2 用完后必须强制 finalize。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("compute", {"expr": "1 + 1"}),
            _plan_tool("compute", {"expr": "2 + 2"}),
            # plan 节点本来还想再要工具，但 call_tool 已检测到 iter_count >= max_iters，
            # 会绕开 plan 直接走 finalize_forced，所以再无 plan 调用。
            _finalize("not_verifiable", comment="强制收尾"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=2)
    assert res.stats.forced_finalize is True
    assert res.stats.iter_count == 2
    assert len(res.record.computation_trace) == 2
    assert res.record.cost["forced_finalize"] is True


# ============== 4. JSON 解析失败兜底 ==============


def test_plan_json_parse_failure_falls_to_reasoner(tmp_path: Path) -> None:
    """plan 第一轮回非法 JSON → reasoner 重试 → 解析成功。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            {"step": "plan", "text": "this is not json", "model": "deepseek-chat"},
            {
                "step": "plan",
                "text": json.dumps({"action": "finalize", "rationale": "reasoner ok"}),
                "model": "deepseek-reasoner",
            },
            _finalize("not_verifiable"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=1)  # rescue 不触发
    assert res.record.verdict == Verdict.NOT_VERIFIABLE
    # chat_calls 应包含两轮 plan + 一轮 finalize
    assert res.stats.chat_calls == 3


def test_plan_double_failure_falls_to_default(tmp_path: Path) -> None:
    """plan chat + reasoner 都 garbage → default {action: finalize}。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            {"step": "plan", "text": "garbage 1", "model": "deepseek-chat"},
            {"step": "plan", "text": "garbage 2", "model": "deepseek-reasoner"},
            _finalize("not_verifiable", comment="default-fallback finalize"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=1)  # rescue 不触发
    assert res.record.verdict == Verdict.NOT_VERIFIABLE
    assert res.stats.chat_calls == 3


def test_finalize_double_failure_falls_to_default(tmp_path: Path) -> None:
    """finalize 两轮都 garbage → default not_verifiable，confidence=0。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            {"step": "finalize", "text": "garbage A", "model": "deepseek-chat"},
            {"step": "finalize", "text": "garbage B", "model": "deepseek-reasoner"},
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=1)  # rescue 不触发（iter=1>=1）
    assert res.record.verdict == Verdict.NOT_VERIFIABLE
    assert res.record.confidence == 0.0
    assert "fallback" in res.record.comment.lower()


def test_finalize_invalid_verdict_clamped(tmp_path: Path) -> None:
    """finalize 返回非法 verdict → 回落 not_verifiable。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            {
                "step": "finalize",
                "text": json.dumps(
                    {
                        "verdict": "premature",   # PREMATURE 已被 pipeline 短路，不允许在 finalize 出现
                        "actual_value": None,
                        "confidence": 0.9,
                        "comment": "...",
                    }
                ),
                "model": "deepseek-chat",
            },
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    assert res.record.verdict == Verdict.NOT_VERIFIABLE


def test_finalize_confidence_clamped(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("verified", confidence=2.5),  # >1
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    assert res.record.confidence == 1.0


def test_finalize_with_markdown_fence(tmp_path: Path) -> None:
    """finalize 文本带 ```json fence → reasoner 路径会剥掉。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    payload = json.dumps({"verdict": "verified", "actual_value": 1, "confidence": 0.9, "comment": "fenced"})
    llm = _StubLLM(
        [
            _plan_finalize(),
            {"step": "finalize", "text": "garbage chat", "model": "deepseek-chat"},
            {"step": "finalize", "text": f"```json\n{payload}\n```", "model": "deepseek-reasoner"},
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    assert res.record.verdict == Verdict.VERIFIED


# ============== 5. 异常 + 总成本 ==============


def test_chat_raises_then_reasoner_succeeds(tmp_path: Path, caplog) -> None:
    """chat 网络抛错 → reasoner 兜底成功。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            {"step": "plan", "text": "", "model": "deepseek-chat", "raise": RuntimeError("net down")},
            {
                "step": "plan",
                "text": json.dumps({"action": "finalize", "rationale": "after retry"}),
                "model": "deepseek-reasoner",
            },
            _finalize("verified"),
        ]
    )
    with caplog.at_level(logging.WARNING):
        res = _run(claim, llm=llm, fs=fs)
    assert res.record.verdict == Verdict.VERIFIED


def test_cost_dict_aggregation(tmp_path: Path) -> None:
    """cost 字段累计 prompt/completion/total + cache_hits + iter_count。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            {**_plan_tool("compute", {"expr": "1+1"}), "prompt_tokens": 100,
             "completion_tokens": 30, "total_tokens": 130},
            {**_plan_finalize(), "prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100, "cached": True},
            {**_finalize("verified"), "prompt_tokens": 50, "completion_tokens": 40, "total_tokens": 90},
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    cost = res.record.cost
    assert cost["prompt_tokens"] == 230
    assert cost["completion_tokens"] == 90
    assert cost["total_tokens"] == 320
    assert cost["cache_hits"] == 1
    assert cost["chat_calls"] == 3
    assert cost["iter_count"] == 2
    assert cost["forced_finalize"] is False


# ============== 6. ticker 注入 + 隔离 ==============


def test_ticker_auto_injected_into_query_financials(tmp_path: Path) -> None:
    """LLM 不传 ticker；agent 用 settings.ticker 注入。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("query_financials", {"line_item_canonical": "revenue", "ticker": "WRONG"}),
            # ↑ LLM 哪怕传了 ticker，dispatch 也会忽略，用 agent 的 ticker。
            _plan_finalize(),
            _finalize("verified"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs)
    # 原始 args 保留 ticker（仅用于审计），但实际查询用 agent 的 ticker
    assert res.record.computation_trace[0].args.get("ticker") == "WRONG"
    # 查询走的是真 ticker=000001 → 命中
    assert res.record.computation_trace[0].result["values"]["FY2024"] == 1.0e9


# ============== 7. plan 直接 finalize（零工具调用） ==============


def test_zero_tool_calls(tmp_path: Path) -> None:
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize("无需工具"),
            _finalize("not_verifiable", comment="原文过软"),
        ]
    )
    # max_iters=1：finalize 时 iter_count(1) >= max_iters(1)，rescue 不触发
    res = _run(claim, llm=llm, fs=fs, max_iters=1)
    assert res.record.computation_trace == []
    assert res.stats.iter_count == 1
    assert res.stats.forced_finalize is False


# ============== 8. P4 rescue 机制 ==============


def test_rescue_triggers_on_not_verifiable_with_remaining_budget(tmp_path: Path) -> None:
    """plan→finalize=NV，仍有预算 → rescue 强制再走一轮 plan，总 finalize 次数=2。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    reports = _StubReports(
        hits=[
            ("000001-FY2025-mda-p001", 0.9, {
                "fiscal_period": "FY2025",
                "section": "管理层讨论与分析",
                "section_canonical": "mda",
                "locator": "管理层讨论与分析#1",
                "source_path": "/tmp/2025.html",
            }),
        ],
        texts={"000001-FY2025-mda-p001": "公司 2025 年继续推进相关项目……"},
    )
    llm = _StubLLM(
        [
            _plan_finalize("先收尾试试"),
            _finalize("not_verifiable", comment="证据不足"),
            # rescue 触发，回到 plan
            _plan_tool(
                "query_chunks",
                {"query": "继续推进 OR 持续优化", "fiscal_periods": ["FY2025"]},
                rationale="rescue: 改写检索词重试",
            ),
            _plan_finalize("rescue 后已找到原文"),
            _finalize(
                "partially_verified",
                confidence=0.6,
                comment="rescue 后定性佐证",
                evidence_chunk_ids=["000001-FY2025-mda-p001"],
            ),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, reports=reports, max_iters=4)

    assert res.record.verdict == Verdict.PARTIALLY_VERIFIED
    # rescue 后做了一次 query_chunks
    assert len(res.record.computation_trace) == 1
    assert res.record.computation_trace[0].tool_name == "query_chunks"
    # evidence 被 rescue 后的 finalize 引用回填
    assert len(res.record.evidence) == 1


def test_rescue_does_not_trigger_when_budget_exhausted(tmp_path: Path) -> None:
    """max_iters=1：iter_count(1) >= max_iters(1)，rescue 不触发，直接 NV 收尾。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("not_verifiable", comment="证据不足"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=1)
    assert res.record.verdict == Verdict.NOT_VERIFIABLE
    # 无 rescue 第二轮 finalize
    assert res.stats.chat_calls == 2  # 1 plan + 1 finalize


def test_rescue_does_not_trigger_for_non_nv_verdicts(tmp_path: Path) -> None:
    """verdict != not_verifiable 时 gate 直接 finalize，不会 rescue。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("partially_verified", comment="部分达成"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=4)
    assert res.record.verdict == Verdict.PARTIALLY_VERIFIED
    assert res.stats.chat_calls == 2


def test_rescue_does_not_trigger_in_forced_finalize(tmp_path: Path) -> None:
    """forced_finalize 路径下 rescue 不触发（即便 verdict=NV、即便预算耗尽）。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    llm = _StubLLM(
        [
            _plan_tool("compute", {"expr": "1 + 1"}),
            _plan_tool("compute", {"expr": "2 + 2"}),
            # call_tool 检测到 iter_count >= max_iters，绕开 plan 直接 forced_finalize
            _finalize("not_verifiable", comment="forced 路径"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, max_iters=2)
    assert res.record.verdict == Verdict.NOT_VERIFIABLE
    assert res.stats.forced_finalize is True
    # 没多出一轮 rescue
    assert res.stats.chat_calls == 3


def test_rescue_ceiling_caps_verified_to_partially_verified(tmp_path: Path) -> None:
    """rescue 后 LLM 给 verified 也只能下调成 partially_verified。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    reports = _StubReports(hits=[], texts={})
    llm = _StubLLM(
        [
            _plan_finalize("先 NV 试试"),
            _finalize("not_verifiable", comment="第一轮证据不足"),
            # rescue
            _plan_tool("query_chunks", {"query": "改写"}),
            _plan_finalize("rescue 后给 verified"),
            _finalize("verified", confidence=0.95, comment="重试后给的高判"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, reports=reports, max_iters=4)
    # 即便 LLM 说 verified，rescue 上限 → partially_verified
    assert res.record.verdict == Verdict.PARTIALLY_VERIFIED
    assert "rescue 上限" in res.record.comment


def test_rescue_ceiling_keeps_partially_verified(tmp_path: Path) -> None:
    """rescue 后给 partially_verified 保持原样，不降级为更低。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    reports = _StubReports(hits=[], texts={})
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("not_verifiable", comment="先 NV"),
            _plan_tool("query_chunks", {"query": "改写"}),
            _plan_finalize(),
            _finalize("partially_verified", confidence=0.7, comment="部分达成"),
        ]
    )
    res = _run(claim, llm=llm, fs=fs, reports=reports, max_iters=4)
    assert res.record.verdict == Verdict.PARTIALLY_VERIFIED
    # 不该带 rescue 上限注释（因为没下调）
    assert "rescue 上限" not in res.record.comment


def test_rescue_no_double_trigger(tmp_path: Path) -> None:
    """rescue 之后再次 NV 也不能再 rescue（chunk_retry_done=True 后只允许一次）。"""
    fs = _seed_financials(tmp_path)
    claim = _make_claim()
    reports = _StubReports(hits=[], texts={})
    llm = _StubLLM(
        [
            _plan_finalize(),
            _finalize("not_verifiable", comment="第一轮 NV"),
            # rescue：再走一轮
            _plan_tool("query_chunks", {"query": "改写"}),
            _plan_finalize(),
            _finalize("not_verifiable", comment="rescue 后仍 NV"),
            # 此时 chunk_retry_done=True，gate 不会再 retry，直接收尾
        ]
    )
    res = _run(claim, llm=llm, fs=fs, reports=reports, max_iters=4)
    assert res.record.verdict == Verdict.NOT_VERIFIABLE
    # 总 chat_calls = 5（2 plan + 1 finalize + 1 plan_tool + 1 plan_finalize? 实际 2 plan + 1 finalize + 2 plan + 1 finalize = 6? ）
    # 准确的: plan(1) + finalize(1) + plan_tool(2) + plan_finalize(3) + finalize(2) = 5 chat 调用
    assert res.stats.chat_calls == 5
    # iter_count: plan(1)→1, plan_tool 是 iter=2, plan_finalize 是 iter=3 → 3
    assert res.stats.iter_count == 3
