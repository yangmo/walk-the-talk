"""DeepSeek 客户端：基于 OpenAI SDK，DeepSeek API 是 OpenAI-compatible。

环境变量：
    DEEPSEEK_API_KEY    必填
    DEEPSEEK_BASE_URL   选填，默认 https://api.deepseek.com/v1

模型：
    deepseek-chat       主力，便宜
    deepseek-reasoner   降级用，贵但更稳

使用：
    from walk_the_talk.llm import DeepSeekClient, PromptCache

    client = DeepSeekClient(cache=PromptCache(work_dir / "llm_cache.db"))
    resp = client.chat(
        messages=[{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
        model="deepseek-chat",
        response_format={"type": "json_object"},
    )
    print(resp.text, resp.cached, resp.total_tokens)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from openai import OpenAI

from .cache import PromptCache
from .client import LLMClient, LLMResponse
from .retry import retry_with_backoff

log = logging.getLogger(__name__)


_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekClient(LLMClient):
    """DeepSeek 实现。"""

    name = "deepseek"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        cache: PromptCache | None = None,
        max_attempts: int = 5,
    ):
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "缺少 DEEPSEEK_API_KEY。请在 .env 或环境变量里配置。"
            )
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or os.getenv("DEEPSEEK_BASE_URL") or _DEFAULT_BASE_URL,
        )
        self._cache = cache
        self._max_attempts = max_attempts

    # ============== 主接口 ==============

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
        # 1. 缓存查
        cache_key = None
        if self._cache:
            extras: dict[str, Any] = {"temperature": temperature}
            if max_tokens is not None:
                extras["max_tokens"] = max_tokens
            if response_format is not None:
                extras["response_format"] = response_format
            cache_key = self._cache.make_key(model, messages, extras)
            cached = self._cache.get(cache_key)
            if cached is not None:
                return LLMResponse(
                    text=cached["response_text"],
                    model=cached["model"],
                    prompt_tokens=cached["prompt_tokens"],
                    completion_tokens=cached["completion_tokens"],
                    total_tokens=cached["total_tokens"],
                    cached=True,
                    raw=cached.get("raw", {}),
                )

        # 2. 真发请求（带重试）
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if response_format is not None:
            kwargs["response_format"] = response_format

        completion = retry_with_backoff(
            self._client.chat.completions.create,
            max_attempts=self._max_attempts,
            **kwargs,
        )

        # 3. 解析
        choice = completion.choices[0]
        text = choice.message.content or ""
        usage = getattr(completion, "usage", None)
        pt = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        ct = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        tt = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
        raw_dict = (
            completion.model_dump() if hasattr(completion, "model_dump") else {}
        )

        # 4. 写缓存
        if self._cache and cache_key:
            try:
                self._cache.put(
                    cache_key,
                    model=model,
                    response_text=text,
                    prompt_tokens=pt,
                    completion_tokens=ct,
                    total_tokens=tt,
                    raw=raw_dict,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("写缓存失败（不影响主流程）：%s", e)

        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            cached=False,
            raw=raw_dict,
        )
