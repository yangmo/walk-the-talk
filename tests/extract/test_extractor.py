"""Phase 2 smoke tests: extractor + postprocess（用 mock LLM，不打真实网络）。"""

from __future__ import annotations

import json
from typing import Any

from walk_the_talk.core.enums import SectionCanonical
from walk_the_talk.core.models import Chunk
from walk_the_talk.extract.extractor import extract_from_chunk
from walk_the_talk.extract.postprocess import postprocess_claims
from walk_the_talk.llm.client import LLMClient, LLMResponse  # 直接走基类，绕开 openai 可选依赖

# ============== Mock LLM ==============


class MockLLM(LLMClient):
    """每次按 model 名返回预设 JSON。"""

    name = "mock"

    def __init__(self, responses: dict[str, str]):
        self._responses = responses
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
    ) -> LLMResponse:
        self.calls.append({"model": model, "messages": messages})
        text = self._responses.get(model, '{"claims": []}')
        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cached=False,
        )


# ============== Fixtures ==============


def _make_chunk(text: str = "公司展望未来") -> Chunk:
    return Chunk(
        chunk_id="688981-FY2024-sec02-p001",
        ticker="688981",
        fiscal_period="FY2024",
        section="第二节致股东的信",
        section_canonical=SectionCanonical.MGMT_LETTER,
        source_path="/tmp/2024.html",
        locator="第二节致股东的信#1",
        text=text,
    )


_GOOD_RESPONSE = json.dumps(
    {
        "claims": [
            {
                "claim_type": "quantitative_forecast",
                "speaker": "管理层",
                "original_text": "力争 2025 年研发投入占比不低于 8%",
                "subject": {"scope": "整体", "name": ""},
                "metric": "研发投入占比",
                "metric_canonical": "rd_expense_ratio",
                "predicate": {"operator": ">=", "value": 0.08, "unit": "%"},
                "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
                "conditions": "",
                "hedging_words": ["力争"],
                "specificity_score": 5,
                "verifiability_score": 5,
                "materiality_score": 4,
                "extraction_confidence": 0.9,
                "verification_plan": {
                    "required_line_items": ["rd_expense", "revenue"],
                    "computation": "rd_expense / revenue",
                    "comparison": ">= 0.08",
                },
            }
        ]
    },
    ensure_ascii=False,
)


# ============== Extractor tests ==============


def test_extract_happy_path():
    client = MockLLM({"deepseek-chat": _GOOD_RESPONSE})
    chunk = _make_chunk()
    claims, stats = extract_from_chunk(client, chunk, fiscal_year=2024, seq_start=1)
    assert len(claims) == 1
    c = claims[0]
    assert c.claim_type.value == "quantitative_forecast"
    assert c.metric_canonical == "rd_expense_ratio"
    assert c.predicate.operator == ">="
    assert c.predicate.value == 0.08
    assert c.horizon.start == "FY2025"
    assert c.from_fiscal_year == 2024
    assert c.canonical_key == "rd_expense_ratio|整体|FY2025~FY2025"
    assert stats["fallback_used"] is False
    assert stats["error"] is None


def test_extract_empty_returns_no_claims():
    client = MockLLM({"deepseek-chat": '{"claims": []}'})
    claims, stats = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert claims == []
    assert stats["error"] is None


def test_extract_falls_back_to_reasoner_on_bad_json():
    client = MockLLM(
        {
            "deepseek-chat": "this is not json at all",
            "deepseek-reasoner": _GOOD_RESPONSE,
        }
    )
    claims, stats = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert len(claims) == 1
    assert stats["fallback_used"] is True
    assert stats["used_model"] == "deepseek-reasoner"


def test_extract_handles_fenced_json():
    client = MockLLM({"deepseek-chat": f"```json\n{_GOOD_RESPONSE}\n```"})
    claims, _ = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert len(claims) == 1


def test_extract_total_failure_returns_empty():
    client = MockLLM({"deepseek-chat": "garbage", "deepseek-reasoner": "still garbage"})
    claims, stats = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert claims == []
    assert stats["error"] is not None


def test_extract_empty_metric_canonical_falls_back_to_metric_slug():
    """LLM 没填 metric_canonical 时，应该用 metric 文本做 canonical_key 兜底，
    避免不同 metric 的 claim 撞同一个 |scope|horizon 假撞键。"""
    payload = {
        "claims": [
            {
                "claim_type": "qualitative_judgment",
                "speaker": "管理层",
                "original_text": "2025年市场需求温和增长",
                "subject": {"scope": "整体", "name": ""},
                "metric": "市场需求",
                "metric_canonical": "",  # 故意空
                "predicate": {"operator": "趋势", "value": "温和增长", "unit": None},
                "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
                "conditions": "",
                "hedging_words": [],
                "specificity_score": 3,
                "verifiability_score": 3,
                "materiality_score": 3,
                "extraction_confidence": 0.8,
                "verification_plan": {
                    "required_line_items": [],
                    "computation": None,
                    "comparison": None,
                },
            }
        ]
    }
    client = MockLLM({"deepseek-chat": json.dumps(payload, ensure_ascii=False)})
    claims, _ = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert len(claims) == 1
    c = claims[0]
    # metric_canonical 字段保持原样（空），但 canonical_key 用 metric slug 兜底
    assert c.metric_canonical == ""
    assert c.metric == "市场需求"
    # canonical_key 不能再是 "|整体|FY2025~FY2025" 这种空头
    assert c.canonical_key.startswith("市场需求|")
    assert c.canonical_key == "市场需求|整体|FY2025~FY2025"


def test_extract_no_metric_at_all_uses_no_metric_placeholder():
    """metric 也空 → 兜底成统一占位符，让所有这种垃圾 claim 撞键被 dedup 吃。"""
    payload = {
        "claims": [
            {
                "claim_type": "qualitative_judgment",
                "speaker": "管理层",
                "original_text": "我们将继续努力",
                "subject": {"scope": "整体", "name": ""},
                "metric": "",
                "metric_canonical": "",
                "predicate": {"operator": "=", "value": "继续", "unit": None},
                "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
                "conditions": "",
                "hedging_words": [],
                "specificity_score": 3,
                "verifiability_score": 3,
                "materiality_score": 3,
                "extraction_confidence": 0.5,
                "verification_plan": {
                    "required_line_items": [],
                    "computation": None,
                    "comparison": None,
                },
            }
        ]
    }
    client = MockLLM({"deepseek-chat": json.dumps(payload, ensure_ascii=False)})
    claims, _ = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert len(claims) == 1
    assert claims[0].canonical_key == "_no_metric_|整体|FY2025~FY2025"


def test_extract_filters_hallucinated_hedging_words():
    """LLM 把不在原文里的 hedging word 塞进来时，要剔除（不丢 claim 整体）。"""
    payload = {
        "claims": [
            {
                "claim_type": "qualitative_judgment",
                "speaker": "管理层",
                "original_text": "2025年市场需求温和增长",  # 注意：不含"普遍认为"也不含"力争"
                "subject": {"scope": "整体", "name": ""},
                "metric": "市场需求",
                "metric_canonical": "market_demand",
                "predicate": {"operator": "趋势", "value": "温和增长", "unit": None},
                "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
                "conditions": "",
                "hedging_words": ["温和", "普遍认为", "力争"],  # "温和"在原文，后两个是幻觉
                "specificity_score": 3,
                "verifiability_score": 3,
                "materiality_score": 3,
                "extraction_confidence": 0.8,
                "verification_plan": {
                    "required_line_items": [],
                    "computation": None,
                    "comparison": None,
                },
            }
        ]
    }
    client = MockLLM({"deepseek-chat": json.dumps(payload, ensure_ascii=False)})
    claims, _ = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert len(claims) == 1
    # 只保留实际在原文中出现的 hedging word
    assert claims[0].hedging_words == ["温和"]


def test_extract_handles_non_string_hedging_words_gracefully():
    """LLM 偶尔返回非字符串 hedging（None / dict / int），不能让整条 claim 崩。"""
    payload = {
        "claims": [
            {
                "claim_type": "qualitative_judgment",
                "speaker": "管理层",
                "original_text": "2025年市场需求温和增长",
                "subject": {"scope": "整体", "name": ""},
                "metric": "市场需求",
                "metric_canonical": "market_demand",
                "predicate": {"operator": "趋势", "value": "温和增长", "unit": None},
                "horizon": {"type": "财年", "start": "FY2025", "end": "FY2025"},
                "conditions": "",
                "hedging_words": [None, "", "温和", 42, {"x": 1}],
                "specificity_score": 3,
                "verifiability_score": 3,
                "materiality_score": 3,
                "extraction_confidence": 0.8,
                "verification_plan": {
                    "required_line_items": [],
                    "computation": None,
                    "comparison": None,
                },
            }
        ]
    }
    client = MockLLM({"deepseek-chat": json.dumps(payload, ensure_ascii=False)})
    claims, _ = extract_from_chunk(client, _make_chunk(), fiscal_year=2024, seq_start=1)
    assert len(claims) == 1
    assert claims[0].hedging_words == ["温和"]


# ============== Postprocess tests ==============


def _make_claim(
    *,
    seq=1,
    year=2024,
    section=SectionCanonical.MGMT_LETTER,
    canonical_key=None,
    spec=4,
    mat=4,
    horizon_end="FY2025",
    original_text="力争达成某目标",
):
    from walk_the_talk.core.ids import canonical_key as _ck
    from walk_the_talk.core.models import Claim, Horizon, Predicate, Subject, VerificationPlan

    ck = canonical_key or _ck("rd_expense_ratio", "整体", f"FY{year+1}", horizon_end)
    return Claim(
        claim_id=f"688981-FY{year}-{seq:03d}",
        claim_type="quantitative_forecast",
        section="第二节",
        section_canonical=section,
        original_text=original_text,
        locator="loc",
        subject=Subject(scope="整体", name=""),
        metric="研发投入占比",
        metric_canonical="rd_expense_ratio",
        predicate=Predicate(operator=">=", value=0.08, unit="%"),
        horizon=Horizon(type="财年", start=f"FY{year+1}", end=horizon_end),
        specificity_score=spec,
        verifiability_score=4,
        materiality_score=mat,
        extraction_confidence=0.9,
        from_fiscal_year=year,
        canonical_key=ck,
        verification_plan=VerificationPlan(),
    )


def test_postprocess_drops_blacklist_section():
    claims = [_make_claim(section=SectionCanonical.LEGAL_TEMPLATE)]
    out, stats = postprocess_claims(claims)
    assert out == []
    assert stats.dropped_section_blacklist == 1


def test_postprocess_drops_expired():
    # horizon.end=FY2023 < from_fiscal_year=2024 → expired
    claims = [_make_claim(horizon_end="FY2023")]
    out, stats = postprocess_claims(claims)
    assert out == []
    assert stats.dropped_expired == 1


def test_postprocess_drops_trivial():
    claims = [_make_claim(spec=2, mat=2)]
    out, stats = postprocess_claims(claims)
    assert out == []
    assert stats.dropped_trivial == 1


def test_postprocess_dedup_within_year_keeps_highest_specificity():
    a = _make_claim(seq=1, spec=3)
    b = _make_claim(seq=2, spec=5)  # 同 canonical_key，更高 spec
    out, stats = postprocess_claims([a, b])
    assert len(out) == 1
    assert out[0].specificity_score == 5
    assert stats.dedup_within_year == 1


def test_postprocess_dedup_cross_year_template():
    # 同样的 original_text + 同样的 canonical_key 才视为模板（避免误杀）
    shared_ck = "rd_expense_ratio|整体|FY2030~FY2030"
    a = _make_claim(
        seq=1, year=2023, canonical_key=shared_ck,
        original_text="本公司将在 2030 年前持续推动战略转型",
    )
    b = _make_claim(
        seq=1, year=2024, canonical_key=shared_ck,
        original_text="本公司将在 2030 年前持续推动战略转型",
    )
    out, stats = postprocess_claims([a, b])
    assert len(out) == 1
    assert out[0].from_fiscal_year == 2023  # 留最早
    assert stats.dedup_cross_year == 1
