"""LLM 客户端层。

对外接口：
    LLMClient    抽象基类
    LLMResponse  统一响应模型
    DeepSeekClient  DeepSeek-chat / -reasoner 实现（带缓存 + 重试）
    PromptCache  SQLite-backed (prompt_hash, model) → response 缓存

DeepSeekClient 走懒导入：只有在显式引用 walk_the_talk.llm.DeepSeekClient
时才会 import openai，避免无 LLM 的下游测试被强制装 openai。
"""

from .cache import PromptCache
from .client import LLMClient, LLMResponse

__all__ = [
    "LLMClient",
    "LLMResponse",
    "DeepSeekClient",
    "PromptCache",
]


def __getattr__(name: str):
    if name == "DeepSeekClient":
        from .deepseek import DeepSeekClient
        return DeepSeekClient
    raise AttributeError(f"module 'walk_the_talk.llm' has no attribute {name!r}")
