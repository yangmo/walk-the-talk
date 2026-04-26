"""Tests for ingest.chunker and section_canonical."""

from __future__ import annotations

from pathlib import Path

import pytest

from walk_the_talk.core.enums import SectionCanonical
from walk_the_talk.core.models import ParsedReport, Section
from walk_the_talk.ingest import chunk_report, classify_section, load_html
from walk_the_talk.ingest.chunker import (
    DEFAULT_MAX_SIZE,
    chunk_section,
)

# ============== section_canonical ==============


@pytest.mark.parametrize(
    "title,expected",
    [
        ("第一节释义", SectionCanonical.LEGAL_TEMPLATE),
        ("第二节致股东的信", SectionCanonical.MGMT_LETTER),
        ("第三节公司简介和主要财务指标", SectionCanonical.OTHER),
        ("第四节管理层讨论与分析", SectionCanonical.MDA),
        ("第五节董事会报告", SectionCanonical.BOARD_REPORT),
        ("第六节公司治理、环境和社会", SectionCanonical.GOVERNANCE),  # 治理优先
        ("第七节重要事项", SectionCanonical.LEGAL_TEMPLATE),
        ("第八节股份变动及股东情况", SectionCanonical.SHARES),
        ("第九节财务报告", SectionCanonical.NOTES),
        ("可持续发展报告", SectionCanonical.ESG),
        ("不存在的章节", SectionCanonical.OTHER),
        ("", SectionCanonical.OTHER),
    ],
)
def test_classify_section(title: str, expected: SectionCanonical):
    assert classify_section(title) == expected


# ============== chunker 单元测试（合成数据） ==============


def _make_section(title: str, text: str, seq: int = 0) -> Section:
    return Section(seq=seq, title=title, text=text)


def test_chunk_short_section_one_chunk():
    s = _make_section("第二节致股东的信", "短短一段。")
    chunks = chunk_section(s, ticker="688981", fiscal_year=2025, source_path="x.html")
    assert len(chunks) == 1
    assert chunks[0].text == "短短一段。"
    assert chunks[0].section_canonical == SectionCanonical.MGMT_LETTER
    assert chunks[0].locator == "第二节致股东的信#0"
    assert chunks[0].chunk_id.endswith("-p000")
    assert chunks[0].fiscal_period == "FY2025"


def test_chunk_table_only_paragraph_isolated():
    text = "前面一段文字。\n\n[[TABLE_PLACEHOLDER_3]]\n\n后面一段文字。"
    s = _make_section("第三节公司简介和主要财务指标", text)
    chunks = chunk_section(s, ticker="X", fiscal_year=2024, source_path="x")
    # 文本-表格-文本，应至少 3 个 chunk（小段不会被合到表格上）
    assert len(chunks) == 3
    assert chunks[0].text == "前面一段文字。"
    assert chunks[1].text == "[[TABLE_PLACEHOLDER_3]]"
    assert chunks[1].contains_table_refs == ["TABLE_PLACEHOLDER_3"]
    assert chunks[2].text == "后面一段文字。"


def test_chunk_long_paragraph_soft_split():
    # 单段 4000 字，应被句号软切到 max_size 以下
    long_para = "。".join([f"句子{i}" * 30 for i in range(50)]) + "。"
    s = _make_section("第四节管理层讨论与分析", long_para)
    chunks = chunk_section(s, ticker="X", fiscal_year=2024, source_path="x")
    assert all(len(c.text) <= DEFAULT_MAX_SIZE for c in chunks), [(len(c.text), c.locator) for c in chunks]
    assert len(chunks) >= 2


def test_chunk_greedy_merge_short_paragraphs():
    # 10 个短段，每段 100 字，合并后应少于 10 chunk
    paras = [f"段落{i}" + "字" * 100 for i in range(10)]
    text = "\n\n".join(paras)
    s = _make_section("第二节致股东的信", text)
    chunks = chunk_section(s, ticker="X", fiscal_year=2024, source_path="x")
    assert len(chunks) < 10
    # paragraph_seq 应 0-based 连续递增
    seqs = [int(c.locator.split("#")[1]) for c in chunks]
    assert seqs == list(range(len(chunks)))


def test_short_title_attached_to_following_table():
    """短标题段（不以句号结尾）应贴到下一个表格上当 caption。"""
    text = "前面长段一段一段长长的内容内容。\n\n3.研发投入情况表\n\n[[TABLE_PLACEHOLDER_45]]\n\n后面还有内容内容内容。"
    s = _make_section("第四节管理层讨论与分析", text)
    chunks = chunk_section(
        s,
        ticker="X",
        fiscal_year=2024,
        source_path="x",
        min_size=10,
    )
    # 表格 chunk 应包含标题 caption
    table_chunks = [c for c in chunks if "[[TABLE_PLACEHOLDER_45]]" in c.text]
    assert len(table_chunks) == 1
    tc = table_chunks[0]
    assert "3.研发投入情况表" in tc.text
    assert tc.contains_table_refs == ["TABLE_PLACEHOLDER_45"]
    # 不应再有独立的「3.研发投入情况表」chunk
    assert not any(c.text.strip() == "3.研发投入情况表" for c in chunks)


def test_chunk_table_refs_extracted_from_inline():
    # 段内嵌入占位符（不独占行）
    text = "前文 [[TABLE_PLACEHOLDER_5]] 后文。\n\n下一段引用 [[TABLE_PLACEHOLDER_7]]。"
    s = _make_section("第四节管理层讨论与分析", text)
    chunks = chunk_section(s, ticker="X", fiscal_year=2024, source_path="x")
    all_refs = [r for c in chunks for r in c.contains_table_refs]
    assert "TABLE_PLACEHOLDER_5" in all_refs
    assert "TABLE_PLACEHOLDER_7" in all_refs


def test_chunk_empty_section_yields_nothing():
    s = _make_section("空节", "   \n\n  ")
    chunks = chunk_section(s, ticker="X", fiscal_year=2024, source_path="x")
    assert chunks == []


def test_chunk_id_global_uniqueness_across_sections():
    rp = ParsedReport(
        ticker="X",
        fiscal_year=2024,
        source_path="x.html",
        encoding="gbk",
        sections=[
            _make_section("第一节A", "内容一" * 50, seq=0),
            _make_section("第二节B", "内容二" * 50, seq=1),
            _make_section("第三节C", "内容三" * 50, seq=2),
        ],
        tables=[],
    )
    chunks = chunk_report(rp)
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"


# ============== 端到端：SMIC 2025 ==============


def test_chunk_smic_2025_endtoend(smic_html_path: Path):
    if not smic_html_path.exists():
        pytest.skip("SMIC fixture missing")
    rp = load_html(smic_html_path)
    chunks = chunk_report(rp)

    # 总数合理：~200-1000 区间
    assert 100 < len(chunks) < 2000, f"chunk count out of range: {len(chunks)}"

    # 所有 chunk_id 唯一
    ids = [c.chunk_id for c in chunks]
    assert len(ids) == len(set(ids))

    # canonical 覆盖率：应至少 5/9 节命中非 OTHER
    seen_canon = {c.section_canonical for c in chunks}
    seen_canon.discard(SectionCanonical.OTHER)
    assert len(seen_canon) >= 5, f"too few canonical types: {seen_canon}"

    # 每个 chunk 长度应在 [1, max_size] 之间（表格 chunk 可以 < min_size）
    assert all(0 < len(c.text) <= DEFAULT_MAX_SIZE for c in chunks)

    # MDA 章节至少有一个 chunk
    mda_chunks = [c for c in chunks if c.section_canonical == SectionCanonical.MDA]
    assert len(mda_chunks) >= 5, f"MDA chunks: {len(mda_chunks)}"

    # 财务报告（NOTES）chunks 数应占大头
    notes_chunks = [c for c in chunks if c.section_canonical == SectionCanonical.NOTES]
    assert len(notes_chunks) > len(mda_chunks), f"notes={len(notes_chunks)}, mda={len(mda_chunks)}"

    # 含表格占位符的 chunk 全部应有 contains_table_refs
    has_placeholder = [c for c in chunks if "[[TABLE_PLACEHOLDER_" in c.text]
    assert all(c.contains_table_refs for c in has_placeholder)
