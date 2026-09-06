"""Stable abstraction for LLM providers."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
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

    def set_event_sink(self, sink: EventSink | None) -> None:
        """Bind the run event sink so the pipeline can append retry events to the book log."""
        with self._event_sink_lock:
            self._event_sink = sink

    def _emit_event(self, event: str, **data: Any) -> None:
        """Emit provider events safely across threads; logging failures must not hide model
        exceptions.
        """
        with self._event_sink_lock:
            sink = self._event_sink
            if sink is None:
                return
            try:
                sink(event, **data)
            except Exception:  # noqa: BLE001 - Observability failures must not change model-call semantics.
                _LOGGER.exception("Failed to write LLM event: %s", event)

    def usage_summary(self) -> dict[str, Any]:
        """Return cumulative token usage with totals, tiers and cache hit rates."""
        return self.usage.summary()

    def validate_credentials(self) -> None:
        """Validate provider credentials; local and test providers are exempt by default."""

    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        """Return plain model response text; stage is used only for usage attribution."""
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> Any:
        """Request and parse JSON output."""
        text = self.complete(
            messages, tier=tier, json_mode=True, max_tokens=max_tokens, stage=stage
        )
        return parse_json_loose(text)
