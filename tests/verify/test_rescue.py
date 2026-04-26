"""Unit tests for verify/rescue.py (P4 rescue gate + ceiling).

覆盖 :mod:`walk_the_talk.verify.rescue` 三个公开 API 的边界条件：

- :func:`gate_finalize` 触发 / 不触发的所有矩阵
- :func:`enforce_rescue_ceiling` 对各 verdict 的处理
- :data:`RESCUE_RETRY_MESSAGE` 关键字校验（只是断言常量没空）

这些都是纯函数 + 数据结构测试，不依赖 LLM / DB / 文件 IO。
"""

from __future__ import annotations

import pytest

from walk_the_talk.core.enums import Verdict
from walk_the_talk.core.models import VerificationRecord
from walk_the_talk.verify.rescue import (
    RESCUE_RETRY_MESSAGE,
    enforce_rescue_ceiling,
    gate_finalize,
)

# ============== gate_finalize 矩阵 ==============


def _make_state(
    *,
    chunk_retry_done: bool = False,
    iter_count: int = 1,
    max_iters: int = 4,
) -> dict:
    """便捷地构造一个状态 dict（rescue gate 只看这三个 key）。"""
    return {
        "chunk_retry_done": chunk_retry_done,
        "iter_count": iter_count,
        "max_iters": max_iters,
    }


def test_gate_finalize_triggers_rescue_on_not_verifiable():
    """初始 NOT_VERIFIABLE + 仍有预算 + 未重试 → retry。"""
    state = _make_state(chunk_retry_done=False, iter_count=2, max_iters=4)
    assert gate_finalize(state, {"verdict": "not_verifiable"}) == "retry"


def test_gate_finalize_skips_rescue_when_already_retried():
    """已经做过 chunk 重试就不再触发，避免无限循环。"""
    state = _make_state(chunk_retry_done=True)
    assert gate_finalize(state, {"verdict": "not_verifiable"}) == "finalize"


@pytest.mark.parametrize("verdict", ["verified", "partially_verified", "failed", "expired"])
def test_gate_finalize_skips_rescue_for_non_not_verifiable(verdict: str):
    """rescue 只为 NOT_VERIFIABLE 设计；其他 verdict 直接收尾。"""
    state = _make_state(chunk_retry_done=False, iter_count=1, max_iters=4)
    assert gate_finalize(state, {"verdict": verdict}) == "finalize"


def test_gate_finalize_skips_rescue_when_budget_exhausted():
    """没预算了（iter_count >= max_iters）就不再 retry。"""
    state = _make_state(chunk_retry_done=False, iter_count=4, max_iters=4)
    assert gate_finalize(state, {"verdict": "not_verifiable"}) == "finalize"


def test_gate_finalize_handles_uppercase_verdict():
    """LLM 偶尔大写 verdict，应能正确归一化。"""
    state = _make_state()
    assert gate_finalize(state, {"verdict": "NOT_VERIFIABLE"}) == "retry"


def test_gate_finalize_handles_missing_verdict():
    """obj 没 verdict key → 当成非 NOT_VERIFIABLE，直接 finalize。"""
    state = _make_state()
    assert gate_finalize(state, {}) == "finalize"


def test_gate_finalize_handles_missing_state_keys():
    """state 缺 key 也不应崩，按默认值（False / 0 / 0）走。"""
    # 缺 max_iters → 默认 0 → iter_count(0) >= max_iters(0) → 不 retry
    assert gate_finalize({"chunk_retry_done": False}, {"verdict": "not_verifiable"}) == "finalize"


# ============== enforce_rescue_ceiling 矩阵 ==============


def _make_record(verdict: Verdict, comment: str = "") -> VerificationRecord:
    return VerificationRecord(
        fiscal_year=2025,
        verdict=verdict,
        target_value=None,
        actual_value=None,
        evidence=[],
        computation_trace=[],
        confidence=0.5,
        comment=comment,
        cost={},
    )


def test_ceiling_downgrades_verified_to_partially_verified():
    rec = _make_record(Verdict.VERIFIED, comment="原 comment")
    out = enforce_rescue_ceiling(rec)
    assert out.verdict == Verdict.PARTIALLY_VERIFIED
    assert "rescue 上限" in out.comment
    assert "原 comment" in out.comment


def test_ceiling_appends_note_to_empty_comment():
    rec = _make_record(Verdict.VERIFIED, comment="")
    out = enforce_rescue_ceiling(rec)
    assert out.verdict == Verdict.PARTIALLY_VERIFIED
    assert out.comment.startswith("[rescue")


@pytest.mark.parametrize(
    "verdict",
    [
        Verdict.PARTIALLY_VERIFIED,
        Verdict.FAILED,
        Verdict.NOT_VERIFIABLE,
        Verdict.PREMATURE,
        Verdict.EXPIRED,
    ],
)
def test_ceiling_passes_through_non_verified(verdict: Verdict):
    """rescue 后的 ceiling 只下调 VERIFIED；其他 verdict 原样返回，不动 comment。"""
    rec = _make_record(verdict, comment="保持不变")
    out = enforce_rescue_ceiling(rec)
    assert out.verdict == verdict
    assert out.comment == "保持不变"


def test_ceiling_truncates_long_comment_to_1000_chars():
    long = "x" * 1500
    rec = _make_record(Verdict.VERIFIED, comment=long)
    out = enforce_rescue_ceiling(rec)
    assert len(out.comment) <= 1000


def test_ceiling_returns_a_copy_does_not_mutate_input():
    rec = _make_record(Verdict.VERIFIED, comment="原始")
    out = enforce_rescue_ceiling(rec)
    assert rec.verdict == Verdict.VERIFIED  # 入参未被改动
    assert out is not rec


# ============== RESCUE_RETRY_MESSAGE 内容 ==============


def test_rescue_retry_message_mentions_key_strategies():
    """message 应包含三条 fallback 策略关键词，以便 LLM 真能学到。"""
    assert "同义" in RESCUE_RETRY_MESSAGE  # 同义词扩展
    assert "fiscal_periods" in RESCUE_RETRY_MESSAGE  # 拓宽时间窗
    assert "top_k" in RESCUE_RETRY_MESSAGE  # 提高 top_k
