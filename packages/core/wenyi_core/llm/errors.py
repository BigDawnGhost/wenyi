"""Safe, provider-independent diagnostics for failed model requests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .json_parser import JsonParseError
from .retrying import (
    EmptyResponseError,
    TruncatedResponseError,
    error_status_code,
    retry_reason,
)


@dataclass(frozen=True)
class ProviderFailure:
    """A diagnostic that never includes provider response or request contents."""

    status_code: int | None
    category: str
    message: str

    def log_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "error_category": self.category,
            "error_message": self.message,
        }
        if self.status_code is not None:
            fields["status_code"] = self.status_code
        return fields


_HTTP_FAILURES = {
    400: ("invalid_request", "The provider rejected the request. Check the model configuration."),
    401: ("authentication_failed", "Provider authentication failed. Check the API key."),
    402: (
        "insufficient_balance",
        "The provider reported insufficient balance. Recharge the account or change provider.",
    ),
    403: ("permission_denied", "Provider access was denied. Check account and model permissions."),
    404: (
        "model_or_endpoint_not_found",
        "The provider model or endpoint was not found. Check the model and endpoint configuration.",
    ),
    408: ("timeout", "The provider request timed out. Try again later."),
    422: ("invalid_request", "The provider rejected the request. Check the model configuration."),
    429: ("rate_limited", "The provider rate limit was reached. Try again later."),
}
_RESPONSE_FAILURES = (
    (TruncatedResponseError, "truncated_response", "The provider response was truncated."),
    (EmptyResponseError, "empty_response", "The provider returned no usable text."),
    (JsonParseError, "invalid_json", "The provider response was not valid JSON."),
    (json.JSONDecodeError, "invalid_json", "The provider response was not valid JSON."),
)


def describe_provider_failure(error: BaseException) -> ProviderFailure:
    """Normalize known failures without inspecting or copying raw error messages."""
    status = error_status_code(error)
    if status in _HTTP_FAILURES:
        category, message = _HTTP_FAILURES[status]
        return ProviderFailure(status, category, message)
    if status is not None:
        if status >= 500:
            return ProviderFailure(
                status, "provider_unavailable", "The provider is unavailable. Try again later."
            )
        return ProviderFailure(status, "http_error", f"The provider returned HTTP {status}.")

    current: BaseException | None = error
    seen: set[int] = set()
    names: list[str] = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        for error_type, category, message in _RESPONSE_FAILURES:
            if isinstance(current, error_type):
                return ProviderFailure(None, category, message)
        current = current.__cause__ or current.__context__

    reason = retry_reason(error)
    if reason == "timeout":
        return ProviderFailure(None, "timeout", "The provider request timed out. Try again later.")
    if reason == "connection":
        return ProviderFailure(
            None, "connection_failed", "Could not connect to the provider. Check connectivity."
        )
    return ProviderFailure(
        None, "unknown_error", f"The provider request failed ({' caused by '.join(names)})."
    )
