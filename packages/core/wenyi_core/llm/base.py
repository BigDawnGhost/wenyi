"""Stable abstraction for LLM providers."""

from __future__ import annotations

import logging
import signal
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from .json_parser import parse_json_loose
from .usage import UsageTracker

Messages = list[dict[str, str]]
EventSink = Callable[..., None]
_LOGGER = logging.getLogger(__name__)


class LLMClient(ABC):
    """Interface implemented by every provider."""

    def __init__(self) -> None:
        """Initialize independent usage accounting and an optional event sink for the provider."""
        self.usage = UsageTracker()
        self._event_sink: EventSink | None = None
        self._event_sink_lock = threading.Lock()
        self._event_observer: ContextVar[EventSink | None] = ContextVar(
            "llm_event_observer", default=None
        )

    def set_event_sink(self, sink: EventSink | None) -> None:
        """Bind the run event sink so the pipeline can append retry events to the book log."""
        with self._event_sink_lock:
            self._event_sink = sink

    @contextmanager
    def capture_events(self, observer: EventSink) -> Iterator[None]:
        """Capture events in this context only; nested scopes restore their parent observer."""
        token = self._event_observer.set(observer)
        try:
            yield
        finally:
            self._event_observer.reset(token)

    def _emit_event(self, event: str, **data: Any) -> None:
        """Emit provider events safely across threads; logging failures must not hide model
        exceptions.
        """
        with self._event_sink_lock:
            sink = self._event_sink
            if sink is not None:
                try:
                    sink(event, **data)
                except Exception:  # noqa: BLE001 - Logging must not change model-call semantics.
                    self._log_event_sink_error(event)
        observer = self._event_observer.get()
        if observer is not None:
            try:
                observer(event, **data)
            except Exception:  # noqa: BLE001 - Capture must not change model-call semantics.
                self._log_event_sink_error(event)

    def _log_event_sink_error(self, event: str) -> None:
        _LOGGER.exception("Failed to write LLM event: %s", event)

    def usage_summary(self) -> dict[str, Any]:
        """Return cumulative token usage with totals, tiers and cache hit rates."""
        return self.usage.summary()

    def request_snapshot(self, operation: str) -> dict[str, Any] | None:
        """Return authoritative replay metadata when supported, never an inferred route."""
        return None

    def validate_credentials(self, operations: Iterable[str] | None = None) -> None:
        """Validate provider credentials; local and test providers are exempt by default."""

    def cancel(self) -> None:
        """Stop waiting and future requests when supported by the client."""

    @contextmanager
    def interrupt_scope(self):
        """Cancel queued model work before a thread pool joins after Ctrl+C."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = signal.getsignal(signal.SIGINT)

        def stop(signum, frame):
            self.cancel()
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, stop)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)

    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        operation: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        """Return model text using the registered operation route."""
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        operation: str,
        max_tokens: int | None = None,
    ) -> Any:
        """Request and parse JSON output."""
        text = self.complete(messages, operation=operation, json_mode=True, max_tokens=max_tokens)
        return parse_json_loose(text)
