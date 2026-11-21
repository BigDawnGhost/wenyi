"""Provider outage classification and automatic resume backoff policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from wenyi_core.llm.retrying import error_status_code, is_resumable_provider_interrupt

AUTO_RESUME_DELAYS_SECONDS: tuple[int, ...] = (60, 120, 300, 600, 900)
AUTO_RESUME_ATTEMPT_PARAM = "_auto_resume_attempt"
_TERMINAL_HTTP_STATUS_CODES = frozenset({400, 401, 403, 404})


def outage_attempt(params: dict[str, Any] | None) -> int:
    """Read the zero-based automatic resume attempt counter from job parameters."""
    if not isinstance(params, dict):
        return 0
    raw = params.get(AUTO_RESUME_ATTEMPT_PARAM, 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def next_auto_resume_delay(attempt: int) -> int:
    """Return the delay in seconds before the next automatic resume attempt."""
    index = min(max(attempt, 0), len(AUTO_RESUME_DELAYS_SECONDS) - 1)
    return AUTO_RESUME_DELAYS_SECONDS[index]


def auto_resume_schedule(attempt: int, *, now: datetime | None = None) -> tuple[int, str]:
    """Compute the backoff delay and UTC ISO timestamp for the next retry."""
    delay_seconds = next_auto_resume_delay(attempt)
    base = now if now is not None else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    next_retry_at = (base + timedelta(seconds=delay_seconds)).isoformat()
    return delay_seconds, next_retry_at


def is_outage_interrupt(error: BaseException) -> bool:
    """Return True when a provider failure should pause and auto-resume instead of failing.

    Configuration and client errors (HTTP 400, 401, 403, 404 and local value/type errors)
    remain terminal so operators fix them instead of looping.
    """
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return False
    status_code = error_status_code(error)
    if status_code is None:
        raw = getattr(error, "status_code", None)
        if isinstance(raw, int):
            status_code = raw
    if status_code in _TERMINAL_HTTP_STATUS_CODES:
        return False
    return is_resumable_provider_interrupt(error)
