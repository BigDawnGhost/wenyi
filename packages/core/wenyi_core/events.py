"""Progress event contracts for adapters around the core callback.

The core reports progress through ``orchestrator.run(progress=cb)`` independently
of the UI. Adapters can wrap callbacks with a :class:`ProgressEmitter`:

- ``RedisEmitter`` in apps/api publishes events for the WebSocket relay.

The core has no Redis dependency; it invokes the injected progress callback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from inspect import signature
from typing import Protocol, cast, runtime_checkable

ProgressFn = Callable[[int, int, str], None]
"""Core progress callback: ``(done, total, label)`` as positional arguments."""

ProgressPayload = dict[str, object]
"""JSON-serializable locating fields, such as ``{"chapter": 3, "batch": 1}``."""


class StructuredProgressFn(Protocol):
    """A progress callback that also accepts an optional fourth positional argument."""

    def __call__(
        self, done: int, total: int, label: str, payload: ProgressPayload | None = None, /
    ) -> None: ...


def emit_progress(
    progress: ProgressFn, done: int, total: int, label: str, *, payload: ProgressPayload
) -> None:
    """Report a structured tick, omitting payload unless the callback accepts it."""
    try:
        signature(progress).bind(done, total, label, payload)
    except (TypeError, ValueError):
        # The signature cannot bind four arguments or is unavailable. Use the base contract.
        pass
    else:
        cast(StructuredProgressFn, progress)(done, total, label, payload)
        return
    progress(done, total, label)


# Event kinds that carry a terminal failure to live subscribers. Anything not
# listed here keeps its existing progress handling, so this stays additive.
ERROR_KIND = "error"


@dataclass
class TranslationEvent:
    """One progress event.

    ``kind`` identifies the event category. ``label`` carries display text from
    the core ProgressFn callback, and ``payload`` holds optional event details.
    """

    project_id: str | None = None
    kind: str = "progress"  # progress | batch | chapter | term | pipeline | log | error
    done: int = 0
    total: int = 0
    label: str = ""
    payload: ProgressPayload | None = None


@runtime_checkable
class ProgressEmitter(Protocol):
    """Interface for publishing progress events."""

    def emit(self, event: TranslationEvent) -> None: ...


def make_progress_fn(
    emitter: ProgressEmitter, project_id: str | None = None, *, kind: str = "progress"
) -> StructuredProgressFn:
    """Wrap a :class:`ProgressEmitter` as a callback that preserves structured ticks.

    The optional payload is published on the event unchanged; adapters decide how
    to expose it.
    """

    def fn(done: int, total: int, label: str, payload: ProgressPayload | None = None) -> None:
        emitter.emit(
            TranslationEvent(
                project_id=project_id,
                kind=kind,
                done=done,
                total=total,
                label=label,
                payload=payload,
            )
        )

    return fn
