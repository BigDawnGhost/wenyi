"""LLM provider 共用的瞬时错误分类、退避等待与重试事件记录。"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from openai import APIConnectionError, APITimeoutError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

_LOGGER = logging.getLogger(__name__)
_RETRYABLE_STATUS_CODES = {408, 409, 429}
_MAX_WAIT_SECONDS = 30.0
_FALLBACK_WAIT = wait_random_exponential(multiplier=1, max=_MAX_WAIT_SECONDS)


class EmptyResponseError(RuntimeError):
    """模型未在标准响应字段返回任何可用文字。"""


def _exception_chain(error: Any) -> Iterator[Any]:
    """沿异常因果链迭代，并防止异常链中的循环引用。"""
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        cause = getattr(current, "__cause__", None)
        current = cause if cause is not None else getattr(current, "__context__", None)


def _response(error: Any) -> Any:
    """返回异常携带的 HTTP 响应（若有）。"""
    return getattr(error, "response", None)


def _header(error: Any, name: str) -> str | None:
    """从异常响应读取单个头字段，缺失或不可读时返回 None。"""
    for item in _exception_chain(error):
        headers = getattr(_response(item), "headers", None)
        getter = getattr(headers, "get", None)
        if not callable(getter):
            continue
        value = getter(name)
        if value is not None:
            return str(value).strip()
    return None


def error_status_code(error: Any) -> int | None:
    """以 duck typing 从 provider 异常及其响应提取 HTTP 状态码。"""
    for item in _exception_chain(error):
        response = _response(item)
        candidates = (
            getattr(item, "status_code", None),
            getattr(response, "status_code", None),
            getattr(response, "status", None),
            getattr(item, "code", None),
        )
        for value in candidates:
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                code = value
            elif isinstance(value, str):
                try:
                    code = int(value)
                except ValueError:
                    continue
            else:
                continue
            if 100 <= code <= 599:
                return code
    return None


def _retry_override(error: Any) -> bool | None:
    """读取 OpenAI 兼容端点的 x-should-retry 显式指令。"""
    value = (_header(error, "x-should-retry") or "").lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def retry_reason(error: Any) -> str | None:
    """返回瞬时错误的稳定原因代码；永久错误返回 None。

    策略与 OpenAI SDK 保持一致：尊重 ``x-should-retry``，重试
    408/409/429/5xx；无状态码时只接受明确的网络、远端协议或超时错误。
    URL、TLS 证书和本地协议配置错误必须立即失败。
    """
    override = _retry_override(error)
    if override is not None:
        return "server_requested_retry" if override else None

    status_code = error_status_code(error)
    if status_code is not None:
        if status_code in _RETRYABLE_STATUS_CODES or status_code >= 500:
            return f"http_{status_code}"
        return None

    chain = list(_exception_chain(error))
    if any(isinstance(item, EmptyResponseError) for item in chain):
        return "empty_response"

    permanent_types = (
        httpx.InvalidURL,
        httpx.LocalProtocolError,
        httpx.UnsupportedProtocol,
        ssl.SSLCertVerificationError,
    )
    if any(isinstance(item, permanent_types) for item in chain):
        return None

    for item in chain:
        if isinstance(item, (TimeoutError, httpx.TimeoutException, APITimeoutError)):
            return "timeout"
        if isinstance(
            item,
            (
                ConnectionError,
                APIConnectionError,
                httpx.NetworkError,
                httpx.ProxyError,
                httpx.RemoteProtocolError,
            ),
        ):
            return "connection"
    return None


def is_retryable_provider_error(error: Any) -> bool:
    """判断 provider 异常是否适合自动重试。"""
    return retry_reason(error) is not None


def _retry_after_seconds(error: Any) -> float | None:
    """解析 Retry-After/retry-after-ms，并把等待限制在安全上限内。"""
    milliseconds = _header(error, "retry-after-ms")
    if milliseconds:
        try:
            return min(_MAX_WAIT_SECONDS, max(0.0, float(milliseconds) / 1000))
        except ValueError:
            pass

    value = _header(error, "retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(_MAX_WAIT_SECONDS, max(0.0, seconds))


def wait_for_provider_retry(retry_state: RetryCallState) -> float:
    """优先服从服务端等待头，否则使用带随机抖动的指数退避。"""
    error = retry_state.outcome.exception() if retry_state.outcome else None
    server_wait = _retry_after_seconds(error)
    if server_wait is not None:
        return server_wait
    return float(_FALLBACK_WAIT(retry_state))


def _request_id(error: Any) -> str | None:
    """提取 provider 请求 ID，便于关联服务端日志。"""
    for item in _exception_chain(error):
        value = getattr(item, "request_id", None)
        if value:
            return str(value)
    return _header(error, "x-request-id") or _header(error, "request-id")


@dataclass(frozen=True)
class RetryReporter:
    """把每次等待及最终耗尽写入标准日志和可选书籍事件流。"""

    provider: str
    tier: str
    stage: str | None
    max_attempts: int
    emit: Callable[..., None]

    def _error_fields(self, error: Any) -> dict[str, Any]:
        """生成不含请求正文、响应正文和密钥的安全错误字段。"""
        return {
            "reason": retry_reason(error) or "not_retryable",
            "error_type": type(error).__name__,
            "status_code": error_status_code(error),
            "request_id": _request_id(error),
        }

    def before_sleep(self, retry_state: RetryCallState) -> None:
        """Tenacity 回调：记录失败次数、下一次尝试和实际等待时长。"""
        error = retry_state.outcome.exception() if retry_state.outcome else None
        wait_seconds = float(retry_state.next_action.sleep if retry_state.next_action else 0.0)
        fields = self._error_fields(error)
        payload = {
            "provider": self.provider,
            "tier": self.tier,
            "stage": self.stage,
            "failed_attempt": retry_state.attempt_number,
            "next_attempt": retry_state.attempt_number + 1,
            "max_attempts": self.max_attempts,
            "wait_seconds": round(wait_seconds, 3),
            "wait_source": (
                "server" if _retry_after_seconds(error) is not None else "exponential_jitter"
            ),
            **fields,
        }
        self.emit("llm_retry_wait", **payload)
        _LOGGER.warning(
            "LLM request retrying: provider=%s stage=%s tier=%s attempt=%s/%s "
            "wait=%.3fs reason=%s error=%s request_id=%s",
            self.provider,
            self.stage or "unknown",
            self.tier,
            retry_state.attempt_number,
            self.max_attempts,
            wait_seconds,
            fields["reason"],
            fields["error_type"],
            fields["request_id"] or "unknown",
        )

    def exhausted(self, error: Any) -> None:
        """记录所有允许尝试均失败；原异常仍由调用方抛出。"""
        fields = self._error_fields(error)
        payload = {
            "provider": self.provider,
            "tier": self.tier,
            "stage": self.stage,
            "attempts": self.max_attempts,
            **fields,
        }
        self.emit("llm_retry_exhausted", **payload)
        _LOGGER.error(
            "LLM retries exhausted: provider=%s stage=%s tier=%s attempts=%s "
            "reason=%s error=%s request_id=%s",
            self.provider,
            self.stage or "unknown",
            self.tier,
            self.max_attempts,
            fields["reason"],
            fields["error_type"],
            fields["request_id"] or "unknown",
        )


def provider_retry(max_retries: int, reporter: RetryReporter):
    """构造所有远端 provider 共用的选择性重试装饰器。"""

    def exhausted(retry_state: RetryCallState):
        """在停止条件命中时记录耗尽，并重新抛出最后一个原始异常。"""
        error = retry_state.outcome.exception() if retry_state.outcome else None
        if error is None:  # pragma: no cover - 仅防御 Tenacity 状态异常
            raise RuntimeError("LLM retry stopped without an exception")
        reporter.exhausted(error)
        raise error

    return retry(
        stop=stop_after_attempt(max(1, max_retries + 1)),
        wait=wait_for_provider_retry,
        retry=retry_if_exception(is_retryable_provider_error),
        before_sleep=reporter.before_sleep,
        retry_error_callback=exhausted,
    )


__all__ = [
    "EmptyResponseError",
    "RetryReporter",
    "error_status_code",
    "is_retryable_provider_error",
    "provider_retry",
    "retry_reason",
    "wait_for_provider_retry",
]
