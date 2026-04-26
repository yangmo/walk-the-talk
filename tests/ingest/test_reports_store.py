"""Tests for embedding / BM25 / ReportsStore.

Sandbox 用 HashEmbedder（0 重依赖），生产用 BGE。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from walk_the_talk.core.enums import SectionCanonical
from walk_the_talk.core.models import Chunk
from walk_the_talk.ingest import (
    HashEmbedder,
    ReportsStore,
    chunk_report,
    load_html,
    make_embedder,
)
from walk_the_talk.ingest._bm25 import BM25Index

# ============== HashEmbedder ==============


def test_hash_embedder_shape_and_determinism():
    emb = HashEmbedder(dim=128)
    assert emb.dim == 128
    assert emb.name == "hash"

    v1 = emb.encode(["集成电路晶圆代工", "管理层讨论与分析"])
    v2 = emb.encode(["集成电路晶圆代工", "管理层讨论与分析"])
    assert v1.shape == (2, 128)
    assert v1.dtype == np.float32
    np.testing.assert_array_equal(v1, v2)


def test_hash_embedder_l2_normalized():
    emb = HashEmbedder(dim=64)
    v = emb.encode(["这是一个非空文本"])
    norm = float(np.linalg.norm(v[0]))
    assert abs(norm - 1.0) < 1e-5


def test_hash_embedder_empty_text_no_nan():
    emb = HashEmbedder(dim=32)
    v = emb.encode([""])
    assert v.shape == (1, 32)
    assert not np.isnan(v).any()


def test_make_embedder_factory():
    e = make_embedder("hash", dim=16)
    assert isinstance(e, HashEmbedder)
    assert e.dim == 16
    with pytest.raises(ValueError):
        make_embedder("unknown")


# ============== BM25Index ==============


def _mk_chunk(cid: str, text: str, section: str = "第四节管理层讨论与分析") -> Chunk:
    return Chunk(
        chunk_id=cid,
        ticker="688981",
        fiscal_period="FY2025",
        section=section,
        section_canonical=SectionCanonical.MDA,
        source_path="x.html",
        locator=f"{section}#0",
        text=text,
    )


def test_bm25_query_basic():
    idx = BM25Index()
    idx.add(
        [
            _mk_chunk("c1", "公司继续投资 12 英寸晶圆产能扩张"),
            _mk_chunk("c2", "研发投入占营业收入的比例持续提升"),
            _mk_chunk("c3", "ESG 治理体系完善"),
        ]
    )
    res = idx.query("12 英寸 晶圆 产能", k=2)
    assert res[0][0] == "c1"
    assert res[0][1] > 0


def test_bm25_where_filter():
    idx = BM25Index()
    idx.add(
        [
            _mk_chunk("c1", "晶圆代工产能", section="第四节管理层讨论与分析"),
            _mk_chunk("c2", "晶圆代工产能", section="第二节致股东的信"),
        ]
    )
    res = idx.query("晶圆", k=10, where={"section": "第二节致股东的信"})
    assert len(res) == 1
    assert res[0][0] == "c2"


def test_bm25_persistence(tmp_path: Path):
    idx = BM25Index()
    idx.add([_mk_chunk("c1", "测试持久化")])
    p = tmp_path / "bm25.pkl"
    idx.save(p)

    loaded = BM25Index.load(p)
    assert loaded.count() == 1
    res = loaded.query("持久化", k=1)
    assert res[0][0] == "c1"


def test_bm25_empty_corpus_returns_empty():
    idx = BM25Index()
    assert idx.query("anything", k=5) == []


# ============== ReportsStore ==============


def test_reports_store_add_and_query(tmp_path: Path):
    chunks = [
        _mk_chunk("c1", "12 英寸晶圆代工产能持续扩张，公司计划新建工厂"),
        _mk_chunk("c2", "研发投入占营收比例约 15%，远高于同行"),
        _mk_chunk("c3", "公司治理结构完善，董事会独立性强"),
    ]
    store = ReportsStore(tmp_path, ticker="688981", embedder=HashEmbedder(dim=64))
    store.add_chunks(chunks)
    assert store.count() == 3

    # dense
    d = store.query_dense("研发投入比例", k=2)
    assert len(d) == 2
    assert all(isinstance(x[0], str) for x in d)

    # BM25
    b = store.query_bm25("研发投入", k=3)
    assert b[0][0] == "c2"

    # hybrid
    h = store.query_hybrid("12 英寸 产能", k=2)
    assert h[0][0] == "c1"


def test_reports_store_persistence(tmp_path: Path):
    """关掉再开还能查到。"""
    chunks = [_mk_chunk("c1", "晶圆代工业务持续扩张")]
    s1 = ReportsStore(tmp_path, ticker="X", embedder=HashEmbedder(dim=32))
    s1.add_chunks(chunks)

    # 重新打开
    s2 = ReportsStore(tmp_path, ticker="X", embedder=HashEmbedder(dim=32))
    assert s2.count() == 1
    assert s2.query_bm25("晶圆", k=1)[0][0] == "c1"


def test_reports_store_where_filter(tmp_path: Path):
    chunks = [
        _mk_chunk("c1", "MDA 内容", section="第四节管理层讨论与分析"),
        _mk_chunk("c2", "财报内容", section="第九节财务报告"),
    ]
    # 改 section_canonical
    chunks[1].section_canonical = SectionCanonical.NOTES
    store = ReportsStore(tmp_path, ticker="Y", embedder=HashEmbedder(dim=32))
    store.add_chunks(chunks)

    res = store.query_bm25("内容", k=5, where={"section_canonical": "notes"})
    assert len(res) == 1
    assert res[0][0] == "c2"


# ============== 端到端：SMIC 2025 ==============


def test_smic_2025_index_and_retrieve(tmp_path: Path, smic_html_path: Path):
    if not smic_html_path.exists():
        pytest.skip("SMIC fixture missing")
    rp = load_html(smic_html_path)
    chunks = chunk_report(rp)

    store = ReportsStore(tmp_path, ticker=rp.ticker, embedder=HashEmbedder(dim=128))
    store.add_chunks(chunks)
    assert store.count() == len(chunks)

    # BM25 应能命中"研发投入"相关 chunk
    res = store.query_bm25("研发投入", k=5)
    assert len(res) > 0
    # 返回的 top 1 应当包含"研发"二字
    top_id = res[0][0]
    top_chunk = next(c for c in chunks if c.chunk_id == top_id)
    assert "研发" in top_chunk.text

    # hybrid 也跑通（不强求精度，只验证没崩）
    h = store.query_hybrid("12 英寸 晶圆 产能", k=5)
    assert len(h) > 0

    # where filter：限定 MDA
    mda_only = store.query_bm25("经营情况", k=10, where={"section_canonical": "mda"})
    assert all(m[2].get("section_canonical") == "mda" for m in mda_only)
