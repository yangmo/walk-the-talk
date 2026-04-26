"""Phase 1 · Ingest — HTML 年报 → chunks + 向量索引 + 财务库。

Public API（按使用频次从高到低）：

文件解析与切分
    :func:`load_html` / :class:`UnsupportedHtmlLayoutError` — 把 ``<year>.html`` 解析成 :class:`~walk_the_talk.core.models.ParsedReport`
    :func:`chunk_report` / :func:`chunk_section` — 把 ParsedReport 切成 chunks
    :func:`classify_section` — 章节标题 → :class:`~walk_the_talk.core.enums.SectionCanonical`

财务表识别与抽取
    :func:`classify_table` / :class:`TableClassification` — 三大表识别
    :func:`extract_from_report` / :func:`extract_lines_from_table` — 表格 → :class:`~walk_the_talk.core.models.FinancialLine`

向量化
    :class:`Embedder` (Protocol) / :class:`BGEEmbedder` / :class:`HashEmbedder` / :func:`make_embedder`

存储
    :class:`ReportsStore` — Chroma + BM25 双索引
    :class:`FinancialsStore` — SQLite 财务库
    :class:`BM25Index` — 关键词索引（一般用 ReportsStore 包装；直查时可单独用）

子模块
    :mod:`walk_the_talk.ingest.taxonomy` — 中文 line item alias + 单位归一
    :mod:`walk_the_talk.ingest.table_dom` — DOM-level <table> → 2D / markdown 助手
"""

from .bm25_index import BM25Index
from .chunker import chunk_report, chunk_section
from .embedding import BGEEmbedder, Embedder, HashEmbedder, make_embedder
from .financials_store import FinancialsStore
from .html_loader import UnsupportedHtmlLayoutError, load_html
from .reports_store import ReportsStore
from .section_canonical import classify_section
from .table_extractor import (
    TableClassification,
    classify_table,
    extract_from_report,
    extract_lines_from_table,
)

__all__ = [
    # 解析与切分
    "UnsupportedHtmlLayoutError",
    "chunk_report",
    "chunk_section",
    "classify_section",
    "load_html",
    # 财务表
    "TableClassification",
    "classify_table",
    "extract_from_report",
    "extract_lines_from_table",
    # 向量化
    "BGEEmbedder",
    "Embedder",
    "HashEmbedder",
    "make_embedder",
    # 存储
    "BM25Index",
    "FinancialsStore",
    "ReportsStore",
]
