"""LLMClient 抽象基类与统一响应模型。

设计：
- chat() 是同步阻塞调用；并发交给上层 ThreadPoolExecutor 控制。
- 入参对齐 OpenAI ChatCompletion (messages + model + 温度等)，方便切换 vendor。
- 出参 LLMResponse 把 token 计数 / cost 一并带回，方便 Phase 3 verifier 落 verdict.cost。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    """LLM 一次调用的统一返回。

    - text: 模型给的纯文本（已去除 reasoning_content 等 vendor-specific 字段）
    - model: 实际使用的模型名（chat 调用时若降级到 reasoner，这里写降级后的）
    - prompt_tokens / completion_tokens / total_tokens: usage
    - cached: True 表示从本地 PromptCache 命中，没有触发网络 / 计费
    - raw: vendor 原始返回（debug 用，业务代码不要依赖）
    """

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    """LLM 客户端抽象。"""

    name: str = "abstract"

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> LLMResponse:
        """发起一次 chat completion。

        messages: OpenAI 格式 [{"role": "system|user|assistant", "content": "..."}]
        response_format: 例如 {"type": "json_object"}，DeepSeek 支持。
        """
        raise NotImplementedError
