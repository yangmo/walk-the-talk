"""单 chunk → list[Claim] 的抽取器。

主流程：
    extract_from_chunk(client, chunk, fiscal_year, seq_offset) -> list[Claim]

1. build_messages -> LLM(deepseek-chat, json_object) -> parse + 校验
2. 失败 → 同 chunk 用 deepseek-reasoner 再试一次（不带 json_object，需要手动剥 ```json fences）
3. 仍失败 → 返回 [] 并把错误日志记下来（不阻断 pipeline）

caller 负责：
- claim_id 分配（pipeline 层做，因为要全 chunk 串起来按序号编）
- 把 LLM 没填的 section / from_fiscal_year / canonical_key 补进 Claim
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from ..core.ids import _slug as _slug_text
from ..core.ids import canonical_key as build_canonical_key
from ..core.ids import claim_id as build_claim_id
from ..core.models import Chunk, Claim, Horizon, Predicate, Subject, VerificationPlan
from ..llm import LLMClient
from .prompts import build_messages

log = logging.getLogger(__name__)

CHAT_MODEL = "deepseek-chat"
REASONER_MODEL = "deepseek-reasoner"

# Reasoner 不一定遵守 json_object，要自己剥 fences
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


# ============== 公开入口 ==============


def extract_from_chunk(
    client: LLMClient,
    chunk: Chunk,
    *,
    fiscal_year: int,
    seq_start: int,
    chat_model: str = CHAT_MODEL,
    reasoner_model: str = REASONER_MODEL,
) -> tuple[list[Claim], dict[str, Any]]:
    """对一个 chunk 跑一次抽取，返回 (claims, stats)。

    stats: {"used_model", "cached", "prompt_tokens", "completion_tokens",
            "total_tokens", "fallback_used", "error"}
    """
    messages = build_messages(
        chunk_text=chunk.text,
        from_fiscal_year=fiscal_year,
        section=chunk.section,
        locator=chunk.locator,
    )

    stats: dict[str, Any] = {
        "used_model": chat_model,
        "cached": False,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "fallback_used": False,
        "error": None,
    }

    # 第一轮：chat + json_object
    raw_claims, err = _try_call(
        client,
        messages,
        model=chat_model,
        json_mode=True,
        temperature=0.0,
        stats=stats,
    )

    # 第二轮：reasoner（不强制 json_object，剥 fences）
    if raw_claims is None:
        log.warning("[%s] chat 抽取失败，降级 reasoner: %s", chunk.chunk_id, err)
        stats["fallback_used"] = True
        stats["used_model"] = reasoner_model
        raw_claims, err = _try_call(
            client,
            messages,
            model=reasoner_model,
            json_mode=False,
            temperature=0.0,
            stats=stats,
        )

    if raw_claims is None:
        stats["error"] = err or "unknown"
        log.error("[%s] reasoner 也失败，丢弃: %s", chunk.chunk_id, err)
        return [], stats

    # 把 raw dict 喂到 Pydantic + 补 caller 字段
    claims: list[Claim] = []
    for i, raw in enumerate(raw_claims):
        try:
            claim = _materialize_claim(
                raw,
                chunk=chunk,
                fiscal_year=fiscal_year,
                seq=seq_start + i,
            )
        except ValidationError as ve:
            log.warning("[%s] claim #%d 校验失败，跳过: %s", chunk.chunk_id, i, ve)
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] claim #%d 转换异常，跳过: %s", chunk.chunk_id, i, e)
            continue
        claims.append(claim)

    return claims, stats


# ============== 内部：LLM 调用 + 解析 ==============


def _try_call(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    model: str,
    json_mode: bool,
    temperature: float,
    stats: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """单次调用 + JSON 解析。返回 (raw_claims_list, err_msg)。"""
    response_format: dict[str, Any] | None = {"type": "json_object"} if json_mode else None
    try:
        resp = client.chat(
            messages,
            model=model,
            temperature=temperature,
            response_format=response_format,
            timeout=120.0,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"

    stats["cached"] = stats["cached"] or resp.cached
    stats["prompt_tokens"] += resp.prompt_tokens
    stats["completion_tokens"] += resp.completion_tokens
    stats["total_tokens"] += resp.total_tokens

    text = resp.text.strip()
    if not text:
        return None, "empty response"

    # 剥 ```json fences
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as je:
        return None, f"JSONDecodeError: {je.msg} @ pos {je.pos}"

    if not isinstance(obj, dict) or "claims" not in obj:
        return None, "missing top-level 'claims' field"
    claims_list = obj["claims"]
    if not isinstance(claims_list, list):
        return None, "'claims' must be a list"
    return claims_list, None


# ============== 内部：raw dict → Claim ==============


def _materialize_claim(
    raw: dict[str, Any],
    *,
    chunk: Chunk,
    fiscal_year: int,
    seq: int,
) -> Claim:
    """把 LLM 的原始 dict 补全 caller-side 字段后构造成 Claim。"""
    subj_raw = raw.get("subject") or {}
    subject = Subject(
        scope=str(subj_raw.get("scope", "整体")),
        name=str(subj_raw.get("name", "")),
    )
    pred_raw = raw.get("predicate") or {}
    predicate = Predicate(
        operator=str(pred_raw.get("operator", "=")),
        value=pred_raw.get("value"),
        unit=pred_raw.get("unit"),
    )
    hor_raw = raw.get("horizon") or {}
    horizon = Horizon(
        type=str(hor_raw.get("type", "财年")),
        start=str(hor_raw.get("start", f"FY{fiscal_year}")),
        end=str(hor_raw.get("end", f"FY{fiscal_year}")),
    )
    plan_raw = raw.get("verification_plan") or {}
    plan = VerificationPlan(
        required_line_items=list(plan_raw.get("required_line_items") or []),
        computation=plan_raw.get("computation"),
        comparison=plan_raw.get("comparison"),
    )

    # metric / metric_canonical：空 metric_canonical 用 metric 文本 slug 做 fallback，
    # 避免不同 metric 的 claim 共享 "|scope|horizon" 假撞 canonical_key
    metric_text = str(raw.get("metric", "") or "").strip()
    metric_canonical = str(raw.get("metric_canonical", "") or "").strip()
    canonical_for_key = metric_canonical or _slug_text(metric_text) or "_no_metric_"

    # canonical_key
    subject_canonical = subject.name or subject.scope
    ck = build_canonical_key(canonical_for_key, subject_canonical, horizon.start, horizon.end)

    # hedging_words 校验：剔除原文里不存在的 hedging（消除 LLM 幻觉，如把"普遍认为"安插进去）
    original_text = str(raw.get("original_text", ""))
    raw_hedging = list(raw.get("hedging_words") or [])
    hedging_words = [w for w in raw_hedging if isinstance(w, str) and w and w in original_text]

    return Claim(
        claim_id=build_claim_id(chunk.ticker, fiscal_year, seq),
        claim_type=str(raw["claim_type"]),  # Pydantic 会做 enum 校验
        section=chunk.section,
        section_canonical=chunk.section_canonical,
        speaker=str(raw.get("speaker", "管理层") or "管理层"),
        original_text=original_text,
        locator=chunk.locator,
        subject=subject,
        metric=metric_text,
        metric_canonical=metric_canonical,
        predicate=predicate,
        horizon=horizon,
        conditions=str(raw.get("conditions", "") or ""),
        hedging_words=hedging_words,
        specificity_score=int(raw.get("specificity_score", 1) or 1),
        verifiability_score=int(raw.get("verifiability_score", 1) or 1),
        materiality_score=int(raw.get("materiality_score", 1) or 1),
        extraction_confidence=float(raw.get("extraction_confidence", 0.0) or 0.0),
        from_fiscal_year=fiscal_year,
        canonical_key=ck,
        verification_plan=plan,
    )
