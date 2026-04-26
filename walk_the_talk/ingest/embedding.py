"""Embedding 抽象与两个实现。

- `BGEEmbedder`：生产用 BAAI/bge-small-zh-v1.5（512 维），需 sentence-transformers
- `HashEmbedder`：jieba 分词 + hashing trick，0 重依赖，CI / sandbox / fallback 用

design choice：把 embedder 做成 Protocol 是因为 sentence-transformers 拉 torch 太大，
不能强制依赖。生产代码用 BGE，测试代码用 Hash，下游 ReportsStore 不感知差别。

用法：
    emb = make_embedder("bge")
    vecs = emb.encode(["文本一", "文本二"])  # -> np.ndarray, shape (2, dim)
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Embedder(Protocol):
    """所有 embedder 的最小接口。"""

    name: str

    @property
    def dim(self) -> int: ...

    def encode(self, texts: list[str]) -> np.ndarray: ...


# ============== HashEmbedder（0 重依赖） ==============


class HashEmbedder:
    """jieba 分词 + signed hashing trick，确定性、无网络依赖。

    用途：CI、sandbox、fallback。不指望它有真正的语义近似能力，
    但确保下游 pipeline 可跑通，且相同输入永远产出相同向量。

    没装 jieba 时退化为 split() 分词。
    """

    name = "hash"

    def __init__(self, dim: int = 256):
        self._dim = dim
        try:
            import jieba

            self._jieba = jieba
        except ImportError:
            self._jieba = None

    @property
    def dim(self) -> int:
        return self._dim

    def _tokenize(self, text: str) -> list[str]:
        if self._jieba is None:
            return text.split()
        return [t for t in self._jieba.cut(text) if t.strip()]

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self._dim), dtype="float32")
        for i, text in enumerate(texts):
            for tok in self._tokenize(text):
                h = hashlib.md5(tok.encode("utf-8")).digest()
                idx = int.from_bytes(h[:4], "little") % self._dim
                sign = 1.0 if (h[4] & 1) else -1.0
                out[i, idx] += sign
            n = float(np.linalg.norm(out[i]))
            if n > 0:
                out[i] /= n
        return out


# ============== BGEEmbedder（生产） ==============


class BGEEmbedder:
    """sentence-transformers 加载 BAAI/bge-small-zh-v1.5（512 维）。

    单例 lazy-init：第一次 encode 时才下载/加载模型。
    """

    name = "bge-small-zh-v1.5"
    _model = None  # 类级单例

    def __init__(self, model_id: str = "BAAI/bge-small-zh-v1.5", dim: int = 512):
        self._model_id = model_id
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _ensure_loaded(self):
        if BGEEmbedder._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                raise RuntimeError(
                    "sentence-transformers 未安装。`pip install sentence-transformers` "
                    "或在 config.yaml 里把 embedder 切到 'hash'。"
                ) from e
            BGEEmbedder._model = SentenceTransformer(self._model_id)
        return BGEEmbedder._model

    def encode(self, texts: list[str]) -> np.ndarray:
        m = self._ensure_loaded()
        vecs = m.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return vecs.astype("float32")


# ============== 工厂 ==============


def make_embedder(name: str = "bge", **kwargs) -> Embedder:
    if name in ("bge", "bge-small-zh", "bge-small-zh-v1.5"):
        return BGEEmbedder(**kwargs)
    if name == "hash":
        return HashEmbedder(**kwargs)
    raise ValueError(f"unknown embedder: {name!r}")
