"""Provider outage classification and automatic resume backoff policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from wenyi_core.llm.retrying import error_status_code, retry_reason

# Waits before each automatic resume; together they cover about 15 minutes. A resume that
# gets any model response resets the streak, so only an outage that answers nothing runs out.
AUTO_RESUME_DELAYS_SECONDS: tuple[int, ...] = (60, 120, 240, 480)
MAX_AUTO_RESUME_ATTEMPTS = len(AUTO_RESUME_DELAYS_SECONDS)
AUTO_RESUME_ATTEMPT_PARAM = "_auto_resume_attempt"
# Payment (402) stays terminal: waiting does not top up a balance.
_TERMINAL_HTTP_STATUS_CODES = frozenset({400, 401, 402, 403, 404})


class ProviderOutageExhausted(RuntimeError):
    """Automatic resumes ran out while the provider answered no request."""

    code = "provider_unavailable"

    def __init__(self, error: BaseException, *, attempts: int) -> None:
        waited_minutes = round(sum(AUTO_RESUME_DELAYS_SECONDS[:attempts]) / 60)
        super().__init__(
            f"The model provider kept failing for about {waited_minutes} minutes "
            f"({attempts} automatic retries with no successful response). Check the provider, then "
            f"resume the task. Last error: {error}"
        )


def outage_attempt(params: dict[str, Any] | None) -> int:
    """Read the zero-based automatic resume attempt counter from job parameters."""
    if not isinstance(params, dict):
        return 0
    raw = params.get(AUTO_RESUME_ATTEMPT_PARAM, 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def auto_resume_exhausted(attempt: int) -> bool:
    """Return True when ``attempt`` automatic resumes already ran with no model response."""
    return attempt >= MAX_AUTO_RESUME_ATTEMPTS


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

    Only transient failures qualify: 408/409/429/5xx, timeouts, dropped connections and
    empty responses. Configuration, client and payment errors (HTTP 400, 401, 402, 403, 404,
    exhausted balances and local value/type errors) remain terminal so operators fix them
    instead of looping.
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
    return retry_reason(error) is not None
