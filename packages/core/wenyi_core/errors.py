"""Base exception carrying a stable code for cross-layer classification."""

from __future__ import annotations

from typing import ClassVar


class WenyiError(Exception):
    """Base for Wenyi's own failures, with a stable machine-readable code.

    Subclasses declare ``code`` and ``retryable`` as class attributes so a
    caller can branch on them without importing the concrete class.

    The inherited base classes stay in place for now: ``IngestError`` keeps
    ``RuntimeError`` and the busy errors keep ``BlockingIOError``, so existing
    ``except`` clauses keep working while call sites migrate. Removing those
    builtin parents is a later step, not part of introducing this base.
    """

    code: str = "wenyi_error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            # An instance-level override must not leak onto the class.
            self.code = code

    def __str__(self) -> str:
        return self.message
