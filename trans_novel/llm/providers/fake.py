"""Programmable provider for tests and offline workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..base import LLMClient, Messages


class FakeClient(LLMClient):
    """Programmable offline client.
    handler(messages, tier, json_mode) returns a string. Without a handler, return [] in
    JSON mode and empty text otherwise. Tests inject a handler to simulate translation,
    extraction and other tasks.
    """

    def __init__(
        self,
        handler: Callable[[Messages, str, bool], str] | None = None,
    ) -> None:
        """Store an optional response handler and initialize the call log."""
        super().__init__()
        self.handler = handler
        self.calls: list[dict[str, Any]] = []  # Record calls for assertions.

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        """Record the call and return handler output, or a minimal default response when no
        handler exists.
        """
        self.calls.append(
            {
                # Agent loops append transcript entries in later rounds. Save a snapshot so shared mutable
                # message lists cannot retroactively change recorded calls.
                "messages": [dict(message) for message in messages],
                "tier": tier,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
                "stage": stage,
            }
        )
        if self.handler is not None:
            return self.handler(messages, tier, json_mode)
        return "[]" if json_mode else ""
