"""Shared transient-error classification, backoff and retry events for LLM providers."""

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
# Provider stops that should leave Review resumable even when automatic retry gives up.
_RESUMABLE_INTERRUPT_STATUS_CODES = frozenset({402, 408, 409, 429})
_MAX_WAIT_SECONDS = 30.0
_FALLBACK_WAIT = wait_random_exponential(multiplier=1, max=_MAX_WAIT_SECONDS)


class EmptyResponseError(RuntimeError):
    """The model returned no usable text in standard response fields."""


def _exception_chain(error: Any) -> Iterator[Any]:
    """Walk the exception cause chain while guarding against cycles."""
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        cause = getattr(current, "__cause__", None)
        current = cause if cause is not None else getattr(current, "__context__", None)


def _response(error: Any) -> Any:
    """Return the exception's HTTP response if present."""
    return getattr(error, "response", None)


def _header(error: Any, name: str) -> str | None:
    """Read one response header; return None if absent or unreadable."""
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
    """Extract HTTP status from provider exceptions and responses using duck typing."""
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
    """Read the explicit x-should-retry instruction from compatible endpoints."""
    value = (_header(error, "x-should-retry") or "").lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def retry_reason(error: Any) -> str | None:
    """Return a stable transient-error reason, or None for permanent failures.
    Respect x-should-retry and retry 408/409/429/5xx. Without a status code, accept only
    explicit network, remote-protocol or timeout errors. Fail immediately for malformed
    URLs, TLS certificates and local protocol configuration errors.
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
    """Determine whether a provider exception qualifies for automatic retry."""
    return retry_reason(error) is not None


def is_resumable_provider_interrupt(error: Any) -> bool:
    """Return True when a provider failure should keep long Review runs resumable.

    Covers automatic-retry cases plus payment/quota stops such as HTTP 402. Permanent
    local configuration and certificate errors stay False so Review can still finish as
    failed.
    """
    if is_retryable_provider_error(error):
        return True
    status_code = error_status_code(error)
    if status_code is not None and (
        status_code in _RESUMABLE_INTERRUPT_STATUS_CODES or status_code >= 500
    ):
        return True
    message = str(error).lower()
    return "insufficient balance" in message or "insufficient_quota" in message


def _retry_after_seconds(error: Any) -> float | None:
    """Parse Retry-After/retry-after-ms and cap the wait at a safe upper bound."""
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
    """Prefer server retry headers; otherwise use exponential backoff with jitter."""
    error = retry_state.outcome.exception() if retry_state.outcome else None
    server_wait = _retry_after_seconds(error)
    if server_wait is not None:
        return server_wait
    return float(_FALLBACK_WAIT(retry_state))


def _request_id(error: Any) -> str | None:
    """Extract the provider request ID for correlation with server logs."""
    for item in _exception_chain(error):
        value = getattr(item, "request_id", None)
        if value:
            return str(value)
    return _header(error, "x-request-id") or _header(error, "request-id")


@dataclass(frozen=True)
class RetryReporter:
    """Record retry waits and exhaustion in standard logs and optional book events."""

    provider: str
    tier: str
    stage: str | None
    max_attempts: int
    emit: Callable[..., None]

    def _error_fields(self, error: Any) -> dict[str, Any]:
        """Build safe error fields excluding request bodies, response bodies and credentials."""
        return {
            "reason": retry_reason(error) or "not_retryable",
            "error_type": type(error).__name__,
            "status_code": error_status_code(error),
            "request_id": _request_id(error),
        }

    def before_sleep(self, retry_state: RetryCallState) -> None:
        """Tenacity callback recording failed attempts, the next attempt and actual wait
        duration.
        """
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
        """Record exhaustion after every allowed attempt fails; preserve the original
        exception.
        """
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


def provider_retry(max_retries: int, reporter: RetryReporter, *, sleep=None):
    """Build the selective retry decorator shared by remote providers."""

    def exhausted(retry_state: RetryCallState):
        """Record exhaustion at the stop condition and re-raise the last original exception."""
        error = retry_state.outcome.exception() if retry_state.outcome else None
        if error is None:  # pragma: no cover - Defensive guard for invalid Tenacity state.
            raise RuntimeError("LLM retry stopped without an exception")
        reporter.exhausted(error)
        raise error

    return retry(
        stop=stop_after_attempt(max(1, max_retries + 1)),
        wait=wait_for_provider_retry,
        retry=retry_if_exception(is_retryable_provider_error),
        before_sleep=reporter.before_sleep,
        retry_error_callback=exhausted,
        **({"sleep": sleep} if sleep is not None else {}),
    )


__all__ = [
    "EmptyResponseError",
    "RetryReporter",
    "error_status_code",
    "is_resumable_provider_interrupt",
    "is_retryable_provider_error",
    "provider_retry",
    "retry_reason",
    "wait_for_provider_retry",
]
