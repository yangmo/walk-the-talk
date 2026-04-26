"""LLM 调用的重试 / 退避策略。

只针对可重试错误：
    - 429 速率限制
    - 5xx 服务端
    - 网络错误（timeout / connection reset）
4xx (除 429) 是 prompt 问题，重试无意义，直接抛。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


# OpenAI SDK 异常类型在不同版本 import 路径不同；做软导入兜底。
try:
    from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError  # type: ignore

    _RETRYABLE: tuple[type[BaseException], ...] = (
        RateLimitError,
        APITimeoutError,
        APIConnectionError,
    )

    def _is_retryable_apierror(e: BaseException) -> bool:
        if isinstance(e, APIError):
            status = getattr(e, "status_code", None)
            if status is None:
                return False
            return status >= 500
        return False
except ImportError:  # pragma: no cover
    APIError = Exception  # type: ignore[assignment, misc]
    _RETRYABLE = (TimeoutError, ConnectionError)  # type: ignore[assignment]

    def _is_retryable_apierror(e: BaseException) -> bool:
        return False


def is_retryable(e: BaseException) -> bool:
    if isinstance(e, _RETRYABLE):
        return True
    if _is_retryable_apierror(e):
        return True
    return False


def retry_with_backoff(
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: float = 0.3,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    **kwargs: Any,
) -> T:
    """执行 fn(*args, **kwargs)，遇到可重试错误用指数退避重试。

    delay = min(base_delay * 2**attempt, max_delay) * (1 ± jitter)
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if not is_retryable(e):
                raise
            if attempt == max_attempts - 1:
                break
            delay = min(base_delay * (2**attempt), max_delay)
            delay *= 1 + random.uniform(-jitter, jitter)
            delay = max(0.1, delay)
            if on_retry:
                on_retry(attempt + 1, e, delay)
            else:
                log.warning(
                    "LLM 调用第 %d/%d 次失败: %s — %.2fs 后重试",
                    attempt + 1,
                    max_attempts,
                    type(e).__name__,
                    delay,
                )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
