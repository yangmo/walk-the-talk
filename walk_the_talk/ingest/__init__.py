"""Phase 1: Ingest — HTML → ParsedReport → chunks + embeddings + financials。"""

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
    "load_html",
    "UnsupportedHtmlLayoutError",
    "chunk_report",
    "chunk_section",
    "classify_section",
    "Embedder",
    "BGEEmbedder",
    "HashEmbedder",
    "make_embedder",
    "ReportsStore",
    "TableClassification",
    "classify_table",
    "extract_from_report",
    "extract_lines_from_table",
    "FinancialsStore",
]
