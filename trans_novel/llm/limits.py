"""Cooperative cancellation, invocation budgets and shared connection/account permits."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Event

from .configuration import LLMConfig


class RequestStopped(BaseException):
    """Stop the workflow without converting a budget/cancellation into a model fallback."""


@dataclass
class Reservation:
    started: float
    tokens: int
    actual_tokens: int | None = None


class RequestLimits:
    """Share limits across all operations of one invocation; never hold permits in backoff."""

    def __init__(self, config: LLMConfig, *, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.clock = clock
        self.started = clock()
        self.cancelled = Event()
        self._condition = Condition()
        self._active: dict[str, int] = defaultdict(int)
        self._windows: dict[str, deque[Reservation]] = defaultdict(deque)
        self.requests = 0
        self.reserved_tokens = 0

    def cancel(self) -> None:
        self.cancelled.set()
        with self._condition:
            self._condition.notify_all()

    def check(self) -> None:
        if self.cancelled.is_set():
            raise RequestStopped("Model requests cancelled; completed work remains resumable")
        deadline = self.config.budget.deadline_seconds
        if deadline is not None and self.clock() - self.started >= deadline:
            raise RequestStopped("Model request deadline reached; resume with a new deadline")

    def wait_for_retry(self, delay: float) -> None:
        """Wait cooperatively without retaining a provider permit."""
        until = self.clock() + delay
        while True:
            self.check()
            remaining = until - self.clock()
            if remaining <= 0:
                return
            self.cancelled.wait(min(0.1, remaining))

    @contextmanager
    def attempt(
        self, connection: str, estimate: int, emit: Callable[..., None]
    ) -> Iterator[Reservation]:
        provider = self.config.providers[connection]
        quota = self.config.quotas.get(provider.quota_group or "")
        budget = self.config.budget
        if quota and quota.tokens_per_minute and estimate > quota.tokens_per_minute:
            raise RequestStopped(
                "A request exceeds tokens_per_minute; reduce the batch/output limit"
            )
        last_reason = None
        while True:
            self.check()
            with self._condition:
                now = self.clock()
                window = self._windows[provider.quota_group or ""]
                while window and now - window[0].started >= 60:
                    window.popleft()
                if budget.max_requests is not None and self.requests >= budget.max_requests:
                    raise RequestStopped("Model request budget exhausted")
                reason = None
                if (
                    budget.max_tokens is not None
                    and self.reserved_tokens + estimate > budget.max_tokens
                ):
                    if not any(self._active.values()):
                        raise RequestStopped("Model token reservation budget exhausted")
                    reason = "token_budget"
                elif (
                    provider.max_concurrency is not None
                    and self._active[connection] >= provider.max_concurrency
                ):
                    reason = "connection_concurrency"
                elif (
                    quota
                    and quota.requests_per_minute is not None
                    and len(window) >= quota.requests_per_minute
                ):
                    reason = "requests_per_minute"
                elif (
                    quota
                    and quota.tokens_per_minute is not None
                    and sum(r.tokens for r in window) + estimate > quota.tokens_per_minute
                ):
                    reason = "tokens_per_minute"
                if reason is None:
                    reservation = Reservation(now, estimate)
                    self.requests += 1
                    self.reserved_tokens += estimate
                    self._active[connection] += 1
                    if quota:
                        window.append(reservation)
                    break
                changed_reason = reason != last_reason
                if not changed_reason:
                    self._condition.wait(timeout=0.1)
            # Event sinks may take file locks; never call them while holding the permit lock.
            if changed_reason:
                emit("llm_request_waiting", reason=reason)
                last_reason = reason
        try:
            yield reservation
        finally:
            with self._condition:
                if reservation.actual_tokens is not None:
                    self.reserved_tokens += reservation.actual_tokens - reservation.tokens
                    reservation.tokens = reservation.actual_tokens
                self._active[connection] -= 1
                self._condition.notify_all()
