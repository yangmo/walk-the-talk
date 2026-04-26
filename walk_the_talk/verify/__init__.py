"""Phase 3 verify：claims.json + financials.db → verdicts.json。

Public API（按调用层次从外到内）：

- :func:`run_verify` — pipeline 入口，CLI 子命令直接调
- :func:`run_agent` — 单条 claim 的 LangGraph agent；测试 / 自定义 pipeline 用
- :data:`AgentResult` / :data:`AgentStats` — agent 的出参类型
- :func:`compute` / :func:`query_financials` / :func:`query_chunks` — 三个原子工具
- :data:`ChunkSearcher` — query_chunks 接收的最小搜索 Protocol
- :func:`gate_finalize` / :func:`enforce_rescue_ceiling` / :data:`RESCUE_RETRY_MESSAGE`
  — P4 rescue 机制（独立模块 :mod:`walk_the_talk.verify.rescue`）
"""

from .agent import AgentResult, AgentStats, run_agent
from .pipeline import VerifyResult, run_verify
from .rescue import RESCUE_RETRY_MESSAGE, enforce_rescue_ceiling, gate_finalize
from .tools import ChunkSearcher, ComputeError, compute, query_chunks, query_financials

__all__ = [
    "RESCUE_RETRY_MESSAGE",
    "AgentResult",
    "AgentStats",
    "ChunkSearcher",
    "ComputeError",
    "VerifyResult",
    "compute",
    "enforce_rescue_ceiling",
    "gate_finalize",
    "query_chunks",
    "query_financials",
    "run_agent",
    "run_verify",
]
