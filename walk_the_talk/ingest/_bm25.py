"""jieba 分词 + rank_bm25 的关键字检索索引，pickle 持久化。

为什么单独一个模块：BM25 与 dense 是正交检索通道，下游 verifier agent 经常会
按精确关键词（line item 名、人名、产品代号）查表/查段，这种场景 BM25 召回远好于稠密。
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from ..core.models import Chunk


def _tokenize(text: str) -> list[str]:
    """jieba 精确切分，丢空白 token。"""
    return [t for t in jieba.cut(text) if t.strip()]


class BM25Index:
    """轻量 BM25 索引；pickle 持久化整个 corpus + tokens。

    新增 chunks 后 BM25 对象失效，下次 query 时重建（rank_bm25 不支持增量）。
    """

    def __init__(self) -> None:
        self.ids: list[str] = []
        self.docs: list[str] = []
        self.metas: list[dict] = []
        self._tokens: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    # ============== 写 ==============

    def add(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            self.ids.append(c.chunk_id)
            self.docs.append(c.text)
            self.metas.append(
                {
                    "ticker": c.ticker,
                    "fiscal_period": c.fiscal_period,
                    "section": c.section,
                    "section_canonical": str(c.section_canonical),
                    "locator": c.locator,
                    "contains_table_refs": list(c.contains_table_refs),
                }
            )
            self._tokens.append(_tokenize(c.text))
        self._bm25 = None

    # ============== 读 ==============

    def _ensure_index(self) -> BM25Okapi:
        if self._bm25 is None:
            self._bm25 = BM25Okapi(self._tokens)
        return self._bm25

    def query(
        self,
        text: str,
        k: int = 10,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[str, float, dict]]:
        """返回 [(chunk_id, score, meta), ...]，按 score 降序。"""
        if not self._tokens:
            return []
        bm25 = self._ensure_index()
        q_tokens = _tokenize(text)
        if not q_tokens:
            return []
        scores = bm25.get_scores(q_tokens)
        cand_idx = [
            i
            for i in range(len(self.ids))
            if (where is None or all(self.metas[i].get(k_) == v for k_, v in where.items()))
        ]
        cand_idx.sort(key=lambda i: scores[i], reverse=True)
        # 注意：rank_bm25 在 corpus 很小或某词覆盖率极高时可能给出负分，
        # 这只表示"该 token 区分度低"，不应直接排除。按分值排序取 top-k 即可。
        return [(self.ids[i], float(scores[i]), self.metas[i]) for i in cand_idx[:k]]

    # ============== 持久化 ==============

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(
                {
                    "ids": self.ids,
                    "docs": self.docs,
                    "metas": self.metas,
                    "tokens": self._tokens,
                },
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )

    @classmethod
    def load(cls, path: Path) -> BM25Index:
        idx = cls()
        if not path.exists():
            return idx
        with open(path, "rb") as f:
            d = pickle.load(f)
        idx.ids = d["ids"]
        idx.docs = d["docs"]
        idx.metas = d["metas"]
        idx._tokens = d["tokens"]
        return idx

    def count(self) -> int:
        return len(self.ids)
