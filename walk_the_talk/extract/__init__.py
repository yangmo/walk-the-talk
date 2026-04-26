"""Phase 2 extract: chunks → claims.json。

run_extract / ExtractResult 走懒导入：避免在没装 chromadb / openai 的环境
下（比如纯单测）触发上游 store 的导入开销。
"""

from .extractor import extract_from_chunk
from .postprocess import PostprocessStats, postprocess_claims

__all__ = [
    "run_extract",
    "ExtractResult",
    "extract_from_chunk",
    "postprocess_claims",
    "PostprocessStats",
]


def __getattr__(name: str):
    if name in {"run_extract", "ExtractResult"}:
        from . import pipeline as _p

        return getattr(_p, name)
    raise AttributeError(f"module 'walk_the_talk.extract' has no attribute {name!r}")
