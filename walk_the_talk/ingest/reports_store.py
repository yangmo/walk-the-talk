"""ReportsStore：dense (Chroma) + BM25 双索引的统一接口。

落盘结构：
    <persist_dir>/
        chroma/                # Chroma persistent client
        bm25.pkl               # BM25Index pickle

下游 verifier agent 用 retrieve 工具时调 query_hybrid，dense + BM25 混合召回。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import chromadb

from ..core.models import Chunk
from ._bm25 import BM25Index
from .embedding import Embedder, make_embedder

# RRF 融合常数（论文经验值，60 是 BM25/MMR 文献常用）
_RRF_K = 60


def _meta_to_chroma(meta: dict) -> dict:
    """Chroma metadata 只接受 str/int/float/bool。list 序列化成逗号分隔字符串。"""
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, list):
            out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float, str)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


class ReportsStore:
    """`reports_<ticker>` collection + 同 ticker 的 BM25 索引。"""

    COLLECTION_PREFIX = "reports_"

    def __init__(
        self,
        persist_dir: str | Path,
        ticker: str,
        embedder: Embedder | None = None,
    ):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.ticker = ticker
        self.embedder = embedder or make_embedder("hash")

        # Chroma
        self._client = chromadb.PersistentClient(path=str(self.persist_dir / "chroma"))
        coll_name = f"{self.COLLECTION_PREFIX}{ticker}"
        self._coll = self._client.get_or_create_collection(
            name=coll_name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine", "embedder": self.embedder.name},
        )

        # BM25
        self._bm25_path = self.persist_dir / "bm25.pkl"
        self._bm25 = BM25Index.load(self._bm25_path)

    # ============== 写 ==============

    def add_chunks(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        ids = [c.chunk_id for c in chunks]
        docs = [c.text for c in chunks]
        metas = [
            {
                "ticker": c.ticker,
                "fiscal_period": c.fiscal_period,
                "section": c.section,
                "section_canonical": str(c.section_canonical),
                "locator": c.locator,
                "contains_table_refs": list(c.contains_table_refs),
                "source_path": c.source_path,
            }
            for c in chunks
        ]
        embs = self.embedder.encode(docs).tolist()

        self._coll.upsert(
            ids=ids,
            documents=docs,
            embeddings=embs,
            metadatas=[_meta_to_chroma(m) for m in metas],
        )
        self._bm25.add(chunks)
        self._bm25.save(self._bm25_path)

    # ============== 读 ==============

    def query_dense(
        self,
        text: str,
        k: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[str, float, dict]]:
        """返回 [(chunk_id, distance, meta), ...]，distance 越小越相似（cosine）。"""
        emb = self.embedder.encode([text])[0].tolist()
        chroma_where = _meta_to_chroma(where) if where else None
        res = self._coll.query(
            query_embeddings=[emb],
            n_results=k,
            where=chroma_where,
        )
        ids = res["ids"][0] if res.get("ids") else []
        dists = res["distances"][0] if res.get("distances") else [None] * len(ids)
        metas = res["metadatas"][0] if res.get("metadatas") else [{}] * len(ids)
        out: list[tuple[str, float, dict]] = []
        for cid, d, m in zip(ids, dists, metas, strict=True):
            out.append((cid, float(d) if d is not None else 0.0, m or {}))
        return out

    def query_bm25(
        self,
        text: str,
        k: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[str, float, dict]]:
        """返回 [(chunk_id, score, meta), ...]，score 越大越相关。"""
        return self._bm25.query(text, k=k, where=where)

    def query_hybrid(
        self,
        text: str,
        k: int = 10,
        where: dict[str, Any] | None = None,
        alpha: float = 0.5,
    ) -> list[tuple[str, float, dict]]:
        """RRF (Reciprocal Rank Fusion) 简版。

        RRF score = alpha / (K + rank_dense) + (1-alpha) / (K + rank_bm25)
        各召回 2k，再融合取 top-k。
        """
        d = self.query_dense(text, k=k * 2, where=where)
        b = self.query_bm25(text, k=k * 2, where=where)
        d_rank = {item[0]: i + 1 for i, item in enumerate(d)}
        b_rank = {item[0]: i + 1 for i, item in enumerate(b)}
        meta_map: dict[str, dict] = {}
        for x in d:
            meta_map[x[0]] = x[2]
        for x in b:
            meta_map.setdefault(x[0], x[2])

        all_ids = set(d_rank) | set(b_rank)
        big = 1_000_000
        scored: list[tuple[str, float, dict]] = []
        for cid in all_ids:
            score = alpha / (_RRF_K + d_rank.get(cid, big)) + (1 - alpha) / (_RRF_K + b_rank.get(cid, big))
            scored.append((cid, score, meta_map.get(cid, {})))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    # ============== 列出 ==============

    def iter_chunks(
        self,
        *,
        fiscal_periods: list[str] | None = None,
        section_canonicals: list[str] | None = None,
    ) -> list[Chunk]:
        """按可选过滤列出 Chunk（含 text + metadata），用于 Phase 2 / 3 遍历。

        ChromaDB where 表达式：单条用 dict 直传，多条用 $and / $in。
        """
        from ..core.enums import SectionCanonical

        clauses: list[dict[str, Any]] = []
        if fiscal_periods:
            clauses.append({"fiscal_period": {"$in": list(fiscal_periods)}})
        if section_canonicals:
            clauses.append({"section_canonical": {"$in": list(section_canonicals)}})
        where: dict[str, Any] | None = None
        if len(clauses) == 1:
            where = clauses[0]
        elif len(clauses) > 1:
            where = {"$and": clauses}

        kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
        if where is not None:
            kwargs["where"] = where
        res = self._coll.get(**kwargs)

        ids = res.get("ids") or []
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        # chroma 返回的三列长度严格相等（同一组 chunk 的 id/doc/meta），
        # strict=True 让万一 chroma API 改动也能立即暴露。
        out: list[Chunk] = []
        for cid, doc, meta in zip(ids, docs, metas, strict=True):
            meta = meta or {}
            tbl_refs_raw = meta.get("contains_table_refs", "")
            if isinstance(tbl_refs_raw, str):
                tbl_refs = [s for s in tbl_refs_raw.split(",") if s]
            else:
                tbl_refs = list(tbl_refs_raw or [])
            try:
                section_canonical = SectionCanonical(
                    str(meta.get("section_canonical", SectionCanonical.OTHER))
                )
            except ValueError:
                section_canonical = SectionCanonical.OTHER
            out.append(
                Chunk(
                    chunk_id=cid,
                    ticker=str(meta.get("ticker", self.ticker)),
                    fiscal_period=str(meta.get("fiscal_period", "")),
                    section=str(meta.get("section", "")),
                    section_canonical=section_canonical,
                    source_path=str(meta.get("source_path", "")),
                    locator=str(meta.get("locator", "")),
                    text=doc or "",
                    contains_table_refs=tbl_refs,
                )
            )
        return out

    def get_texts(self, ids: list[str]) -> dict[str, str]:
        """按 chunk_id 列表取原文。Phase 3 query_chunks 工具召回后取 snippet 用。

        缺失的 id 不会出现在返回 dict 里。
        """
        if not ids:
            return {}
        res = self._coll.get(ids=list(ids), include=["documents"])
        out_ids = res.get("ids") or []
        out_docs = res.get("documents") or []
        return {cid: (doc or "") for cid, doc in zip(out_ids, out_docs, strict=True)}

    # ============== 杂项 ==============

    def count(self) -> int:
        return self._coll.count()

    def reset(self) -> None:
        """清空当前 ticker 的索引（删 collection + 清 BM25）。慎用。"""
        try:
            self._client.delete_collection(name=f"{self.COLLECTION_PREFIX}{self.ticker}")
        except Exception:
            pass
        self._coll = self._client.get_or_create_collection(
            name=f"{self.COLLECTION_PREFIX}{self.ticker}",
            embedding_function=None,
            metadata={"hnsw:space": "cosine", "embedder": self.embedder.name},
        )
        self._bm25 = BM25Index()
        if self._bm25_path.exists():
            self._bm25_path.unlink()
