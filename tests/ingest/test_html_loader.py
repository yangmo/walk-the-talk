"""Smoke tests for ingest.html_loader against 中芯国际 FY2025."""

from __future__ import annotations

from pathlib import Path

import pytest

from walk_the_talk.ingest import load_html
from walk_the_talk.ingest.html_loader import (
    _is_real_section_header,
    _split_sections,
)


def _require_fixture(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"SMIC fixture missing: {path}")


def test_load_smic_2025_meta(smic_html_path: Path):
    _require_fixture(smic_html_path)
    rp = load_html(smic_html_path)
    assert rp.ticker == "688981"
    assert rp.fiscal_year == 2025
    assert rp.encoding == "gbk"
    assert rp.report_type.value == "annual"


def test_load_smic_2025_sections(smic_html_path: Path):
    _require_fixture(smic_html_path)
    rp = load_html(smic_html_path)
    titles = [s.title for s in rp.sections]
    # 9 节章节都应被识别（释义 / 致股东信 / 公司简介 / MDA / 董事会报告 / 治理 / 重要事项 / 股份变动 / 财报）
    assert len(rp.sections) >= 8, f"got {len(rp.sections)} sections: {titles}"

    # 标志性章节
    assert any("致股东" in t for t in titles), titles
    assert any("管理层讨论与分析" in t for t in titles), titles
    assert any("财务报告" in t for t in titles), titles

    # MDA 内容应至少 1k 字
    mda = next(s for s in rp.sections if "管理层讨论与分析" in s.title)
    assert len(mda.text) > 1000, f"MDA too short: {len(mda.text)}"

    # 章节按 seq 严格递增
    seqs = [s.seq for s in rp.sections]
    assert seqs == sorted(seqs)


def test_load_smic_2025_tables(smic_html_path: Path):
    _require_fixture(smic_html_path)
    rp = load_html(smic_html_path)
    # 中芯国际 2025 年报有约 280+ 张表
    assert len(rp.tables) > 50, f"too few tables: {len(rp.tables)}"

    for tb in rp.tables[:3]:
        assert tb.bbox_anchor and tb.bbox_anchor.startswith("TABLE_PLACEHOLDER_")
        if tb.markdown:  # 部分极简表可能为空
            assert tb.markdown.startswith("|")
        # raw_2d 与 markdown 一致性：有内容时行数 >= 1
        if tb.raw_2d:
            assert len(tb.raw_2d) >= 1

    # 章节内的 table_refs 应能合计出大量占位符
    total_refs = sum(len(s.table_refs) for s in rp.sections)
    assert total_refs > 50, f"sections only contain {total_refs} table refs"


def test_section_text_is_not_polluted_by_table_chars(smic_html_path: Path):
    """章节正文里不应残留 <table> 的列内文字（如 |、---），
    只能是 [[TABLE_PLACEHOLDER_N]] 占位符形式。"""
    _require_fixture(smic_html_path)
    rp = load_html(smic_html_path)
    for s in rp.sections:
        # markdown 表格的分隔行 "|---|---|" 不应出现在 section.text 里
        assert "|---|" not in s.text, f"table markdown leaked into section {s.title}"


# ============== 单元测试：内部函数 ==============


@pytest.mark.parametrize(
    "title_part,expected",
    [
        ("释义", True),
        ("致股东的信", True),
        ("公司简介和主要财务指标", True),
        ("管理层讨论与分析", True),
        # TOC 行
        ("释义 ...... 5", False),
        ("致股东的信 ...... 6", False),
        # 交叉引用
        ("管理层讨论与分析”之“四、风险因素", False),
        # 引号 / 书名号
        ("”之“四、风险因素", False),
        # 空 / 过长
        ("", False),
        ("一" * 40, False),
    ],
)
def test_is_real_section_header(title_part: str, expected: bool):
    assert _is_real_section_header(title_part) is expected


def test_split_sections_basic():
    text = (
        "TOC\n"
        "第一节释义 ...... 5\n"
        "第二节致股东的信 ...... 6\n"
        "\n"
        "第一节释义\n"
        "释义内容…\n"
        "第二节致股东的信\n"
        "致辞内容…\n"
        "末尾。\n"
    )
    secs = _split_sections(text)
    assert len(secs) == 2
    assert secs[0][0] == "第一节释义"
    assert "释义内容" in secs[0][1]
    assert secs[1][0] == "第二节致股东的信"
    assert "致辞内容" in secs[1][1]
    assert "末尾" in secs[1][1]
