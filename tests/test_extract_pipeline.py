"""Phase 2 pipeline 诊断功能测试：debug 落盘 + inspect_chunks。

为了避开可选依赖（chromadb / openai），本测试在导入 pipeline 之前先把它们
塞进 sys.modules 当作 stub；真正的 ReportsStore / ProgressTracker 通过
monkeypatch 替换为内存假对象。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest


# ============== sys.modules stubs（必须在 import pipeline 之前） ==============


def _try_real_or_stub(modname: str) -> None:
    """优先用真实模块；缺失才放空 stub，避免上游 import 报错。"""
    if modname in sys.modules:
        return
    try:
        __import__(modname)
    except Exception:
        sys.modules[modname] = types.ModuleType(modname)


for _m in ("chromadb", "openai", "jieba", "rank_bm25"):
    _try_real_or_stub(_m)

# openai stub（仅当真实 openai 未装时）补齐 DeepSeekClient / retry.py 引用的符号
_openai_mod = sys.modules.get("openai")
if _openai_mod is not None and not hasattr(_openai_mod, "OpenAI"):
    class _StubOpenAIClient:  # noqa: N801
        def __init__(self, *a, **kw): ...
    _openai_mod.OpenAI = _StubOpenAIClient
    class _OpenAIErr(Exception):
        pass
    for _name in (
        "APIConnectionError", "APITimeoutError", "RateLimitError",
        "InternalServerError", "APIStatusError",
    ):
        if not hasattr(_openai_mod, _name):
            setattr(_openai_mod, _name, _OpenAIErr)


# 现在再 import pipeline
from walk_the_talk.config import ExtractSettings  # noqa: E402
from walk_the_talk.core.enums import SectionCanonical  # noqa: E402
from walk_the_talk.core.models import Chunk  # noqa: E402
from walk_the_talk.extract import pipeline as ext_pipeline  # noqa: E402
from walk_the_talk.llm.client import LLMClient, LLMResponse  # noqa: E402


# ============== 共用假对象 ==============


class FakeReportsStore:
    """假的 ReportsStore：iter_chunks 直接返回 ctor 传入的 chunks。"""

    def __init__(self, chunks_by_period: dict[str, list[Chunk]]):
        self._chunks_by_period = chunks_by_period

    def iter_chunks(
        self,
        *,
        fiscal_periods=None,
        section_canonicals=None,
    ) -> list[Chunk]:
        out: list[Chunk] = []
        periods = fiscal_periods or list(self._chunks_by_period.keys())
        for fp in periods:
            for c in self._chunks_by_period.get(fp, []):
                if section_canonicals and str(c.section_canonical) not in section_canonicals:
                    continue
                out.append(c)
        return out


class FakeProgress:
    """假的 ProgressTracker：内存里维护 done set + 已 ingest 年份。"""

    def __init__(self, done_index_years: list[int] | None = None):
        # 模拟 _ProgressData：years[str(year)] = {"index": "done", ...}
        years_map: dict[str, dict[str, str]] = {}
        for y in done_index_years or []:
            years_map[str(y)] = {"index": "done"}
        self.data = types.SimpleNamespace(years=years_map)
        self._done: set[tuple[int, str]] = set()

    def is_done(self, year: int, phase: str) -> bool:
        return (year, phase) in self._done

    def mark_done(self, year: int, phase: str) -> None:
        self._done.add((year, phase))


class MockLLM(LLMClient):
    name = "mock"

    def __init__(self, responses: dict[str, str]):
        self._responses = responses

    def chat(self, messages, *, model, temperature=0.0, max_tokens=None,
             response_format=None, timeout=60.0) -> LLMResponse:
        return LLMResponse(
            text=self._responses.get(model, '{"claims": []}'),
            model=model,
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cached=False,
        )


# ============== Fixtures ==============


_DEFAULT_CHUNK_TEXT = (
    "公司将在2025年继续推进研发投入和产能建设，"
    "力争研发投入占比达到 8%，资本开支与上一年持平，"
    "并持续加强与产业链上下游合作伙伴的战略协同。"
    "这是一段足够长的实质内容，用于绕过 trivial filter。"
)


def _make_chunk(
    *,
    seq: int,
    section: SectionCanonical = SectionCanonical.MGMT_LETTER,
    year: int = 2024,
    text: str = _DEFAULT_CHUNK_TEXT,
) -> Chunk:
    sec_label = section.value
    return Chunk(
        chunk_id=f"688981-FY{year}-{sec_label[:6]}-p{seq:03d}",
        ticker="688981",
        fiscal_period=f"FY{year}",
        section=f"section-{sec_label}",
        section_canonical=section,
        source_path=f"/tmp/{year}.html",
        locator=f"loc#{seq}",
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


@pytest.fixture
def patched_pipeline(monkeypatch, tmp_path):
    """把 ReportsStore / ProgressTracker / make_embedder 替换为内存假对象。"""

    chunks_2024 = [
        _make_chunk(seq=1, section=SectionCanonical.MGMT_LETTER),
        _make_chunk(seq=2, section=SectionCanonical.MDA),
        _make_chunk(seq=3, section=SectionCanonical.RISK),
        # 一个不在候选 section 列表里的 chunk —— inspect 会列出，extract 会跳过
        _make_chunk(seq=4, section=SectionCanonical.NOTES),
    ]
    fake_store = FakeReportsStore({"FY2024": chunks_2024})

    def _fake_store_ctor(persist_dir, ticker, embedder):  # noqa: ARG001
        return fake_store

    fake_progress = FakeProgress(done_index_years=[2024])

    def _fake_progress_ctor(path, ticker, company):  # noqa: ARG001
        return fake_progress

    monkeypatch.setattr(ext_pipeline, "ReportsStore", _fake_store_ctor)
    monkeypatch.setattr(ext_pipeline, "ProgressTracker", _fake_progress_ctor)
    monkeypatch.setattr(ext_pipeline, "make_embedder", lambda name: object())

    return {
        "data_dir": tmp_path,
        "fake_store": fake_store,
        "fake_progress": fake_progress,
    }


# ============== inspect_chunks ==============


def test_inspect_chunks_lists_all_sections(patched_pipeline):
    settings = ExtractSettings(
        data_dir=patched_pipeline["data_dir"],
        ticker="688981",
        company="中芯国际",
    )
    result = ext_pipeline.inspect_chunks(settings)
    assert result.years == [2024]
    assert result.total_chunks == 4
    by_section = result.chunks_by_year_section[2024]
    # 包含候选 section 之外的 NOTES（用来诊断 ingest 覆盖面）
    assert by_section.get("mgmt_letter") == 1
    assert by_section.get("mda") == 1
    assert by_section.get("risk") == 1
    assert by_section.get("notes") == 1


def test_inspect_chunks_explicit_years(patched_pipeline):
    settings = ExtractSettings(
        data_dir=patched_pipeline["data_dir"],
        ticker="688981",
        company="中芯国际",
        years=[2024],
    )
    result = ext_pipeline.inspect_chunks(settings)
    assert result.years == [2024]


def test_inspect_chunks_no_years(patched_pipeline, monkeypatch):
    # 把 progress.data.years 清空 → 没有候选年
    fake_progress = FakeProgress(done_index_years=[])
    monkeypatch.setattr(ext_pipeline, "ProgressTracker", lambda p, t, c: fake_progress)
    settings = ExtractSettings(
        data_dir=patched_pipeline["data_dir"],
        ticker="688981",
        company="中芯国际",
    )
    result = ext_pipeline.inspect_chunks(settings)
    assert result.years == []
    assert result.total_chunks == 0


# ============== run_extract --debug 落盘 ==============


def test_run_extract_debug_dumps_raw_and_log(patched_pipeline):
    settings = ExtractSettings(
        data_dir=patched_pipeline["data_dir"],
        ticker="688981",
        company="中芯国际",
        max_workers=1,  # 单线程，避免 logs 顺序抖动
    )
    client = MockLLM({"deepseek-chat": _GOOD_RESPONSE})
    result = ext_pipeline.run_extract(settings, llm_client=client, debug=True)

    # claims.raw.json 应该存在并包含 FY2024 的 raw claims
    raw_path = settings.work_dir / "claims.raw.json"
    assert raw_path.exists(), "debug=True 应该落 claims.raw.json"
    raw_data = json.loads(raw_path.read_text("utf-8"))
    assert "2024" in raw_data
    # 候选 section（mgmt_letter / mda / risk）共 3 个 chunk × 1 claim = 3
    assert len(raw_data["2024"]) == 3

    # extract_log.jsonl 每行一个 dict
    log_path = settings.work_dir / "extract_log.jsonl"
    assert log_path.exists()
    log_entries = [json.loads(ln) for ln in log_path.read_text("utf-8").splitlines() if ln]
    assert len(log_entries) == 3
    for entry in log_entries:
        assert entry["year"] == 2024
        assert entry["n_claims"] == 1
        assert entry["error"] is None
        assert entry["fallback_used"] is False

    # 累计 per-section 计数应该正确
    assert result.chunks_by_section.get("mgmt_letter") == 1
    assert result.chunks_by_section.get("mda") == 1
    assert result.chunks_by_section.get("risk") == 1
    # NOTES 不在候选 section_canonicals 里，不会进 extract
    assert "notes" not in result.chunks_by_section
    # 每个 chunk 都返回相同 _GOOD_RESPONSE → raw 各 section 各 1
    assert result.raw_claims_by_section.get("mgmt_letter") == 1
    assert result.raw_claims_by_section.get("mda") == 1
    assert result.raw_claims_by_section.get("risk") == 1
    # 三个 raw claim 共享同一 canonical_key，within-year dedup 后只剩 1 个
    # （final 落在哪个 section 取决于排序，不强约束；但总数必须为 1）
    assert sum(result.final_claims_by_section.values()) == 1
    assert result.pp_dedup_within_year == 2


def test_run_extract_no_debug_skips_dump(patched_pipeline):
    settings = ExtractSettings(
        data_dir=patched_pipeline["data_dir"],
        ticker="688981",
        company="中芯国际",
        max_workers=1,
    )
    client = MockLLM({"deepseek-chat": _GOOD_RESPONSE})
    ext_pipeline.run_extract(settings, llm_client=client, debug=False)

    assert not (settings.work_dir / "claims.raw.json").exists()
    assert not (settings.work_dir / "extract_log.jsonl").exists()


def test_run_extract_records_postprocess_breakdown(patched_pipeline):
    settings = ExtractSettings(
        data_dir=patched_pipeline["data_dir"],
        ticker="688981",
        company="中芯国际",
        max_workers=1,
    )
    client = MockLLM({"deepseek-chat": _GOOD_RESPONSE})
    result = ext_pipeline.run_extract(settings, llm_client=client, debug=False)

    # 这些字段都应该是 int（哪怕是 0），表示 postprocess 漏斗已被记录
    assert isinstance(result.pp_dropped_blacklist, int)
    assert isinstance(result.pp_dropped_expired, int)
    assert isinstance(result.pp_dropped_trivial, int)
    assert isinstance(result.pp_dedup_within_year, int)
    assert isinstance(result.pp_dedup_cross_year, int)
    # 没有触发 blacklist / expired / trivial（_GOOD_RESPONSE 是高质量 claim）
    assert result.pp_dropped_blacklist == 0
    assert result.pp_dropped_expired == 0
    assert result.pp_dropped_trivial == 0
    # 三个候选 chunk 拿到三个相同 canonical_key 的 raw claim → within-year dedup=2
    assert result.raw_claims_total == 3
    assert result.pp_dedup_within_year == 2
    assert result.final_claims_total == 1


# ============== Pre-LLM trivial chunk filter ==============


def test_is_trivial_chunk_short_text():
    short = _make_chunk(seq=1, text="一二三四五")
    assert ext_pipeline._is_trivial_chunk(short)


def test_is_trivial_chunk_table_placeholder():
    placeholder = _make_chunk(seq=1, text="[[TABLE_PLACEHOLDER_2]]")
    assert ext_pipeline._is_trivial_chunk(placeholder)


def test_is_trivial_chunk_template_applicable():
    # 实测中芯 sec03-p031 风格："√适用 □不适用" + 一些条目，去空白后 ~78 字 → 应触发 trivial
    template = _make_chunk(
        seq=1,
        text="1. 重大的股权投资\n□适用 √不适用\n2. 重大的非股权投资\n□适用 √不适用\n3. 以公允价值计量的金融资产\n√适用 □不适用",
    )
    assert ext_pipeline._is_trivial_chunk(template)


def test_is_trivial_chunk_substantive_text_kept():
    # 922 字符的实质内容（中芯董事长致辞节选片段），不应触发 trivial
    body = "尊敬的各位股东、投资人：" + "公司将持续推进研发投入。" * 20
    chunk = _make_chunk(seq=1, text=body)
    assert not ext_pipeline._is_trivial_chunk(chunk)


def test_is_trivial_chunk_table_plus_substantive_text_kept():
    """长 chunk 里夹一个 [[TABLE_PLACEHOLDER]]，仍有大量实质文本 → 不丢。"""
    body = "[[TABLE_PLACEHOLDER_1]]" + "公司将在2025年继续拓展产能并加大研发投入。" * 8
    chunk = _make_chunk(seq=1, text=body)
    assert not ext_pipeline._is_trivial_chunk(chunk)


def test_run_extract_skips_trivial_chunks_pre_llm(monkeypatch, tmp_path):
    """在 fixture 里塞 2 个 trivial + 1 个实质 chunk，trivial 不进 LLM、计数加 2。"""
    chunks = [
        _make_chunk(seq=1, section=SectionCanonical.MGMT_LETTER, text="[[TABLE_PLACEHOLDER_2]]"),
        _make_chunk(seq=2, section=SectionCanonical.MDA, text="□适用 √不适用"),
        _make_chunk(
            seq=3,
            section=SectionCanonical.MDA,
            text="2025年公司力争研发投入占比达到 8%，资本开支与上一年持平。" * 5,
        ),
    ]
    fake_store = FakeReportsStore({"FY2024": chunks})
    fake_progress = FakeProgress(done_index_years=[2024])
    monkeypatch.setattr(ext_pipeline, "ReportsStore", lambda persist_dir, ticker, embedder: fake_store)
    monkeypatch.setattr(ext_pipeline, "ProgressTracker", lambda p, t, c: fake_progress)
    monkeypatch.setattr(ext_pipeline, "make_embedder", lambda name: object())

    settings = ExtractSettings(
        data_dir=tmp_path,
        ticker="688981",
        company="中芯国际",
        max_workers=1,
    )
    # 数 LLM 调用次数
    call_count = {"n": 0}

    class CountingMock(LLMClient):
        name = "counting-mock"

        def chat(self, messages, *, model, temperature=0.0, max_tokens=None,
                 response_format=None, timeout=60.0):
            call_count["n"] += 1
            return LLMResponse(
                text=_GOOD_RESPONSE, model=model,
                prompt_tokens=10, completion_tokens=20, total_tokens=30,
                cached=False,
            )

    result = ext_pipeline.run_extract(settings, llm_client=CountingMock(), debug=False)

    # 2 个 trivial chunk 被跳过 → 只剩 1 个 chunk 进 LLM
    assert result.chunks_skipped_trivial == 2
    assert call_count["n"] == 1
    assert result.chunks_total == 1
    # 这 1 个 chunk 出 1 条 raw claim
    assert result.raw_claims_total == 1


def test_run_extract_records_zero_trivial_skip_when_all_substantive(patched_pipeline):
    """fixture 默认 chunks 用了实质内容（_DEFAULT_CHUNK_TEXT），不会被 trivial filter 杀掉。"""
    settings = ExtractSettings(
        data_dir=patched_pipeline["data_dir"],
        ticker="688981",
        company="中芯国际",
        max_workers=1,
    )
    client = MockLLM({"deepseek-chat": _GOOD_RESPONSE})
    result = ext_pipeline.run_extract(settings, llm_client=client, debug=False)
    assert result.chunks_skipped_trivial == 0
    # 候选 section 3 个 chunk 全部进 LLM
    assert result.chunks_total == 3
