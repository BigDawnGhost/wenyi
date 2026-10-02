"""Progress event contracts for adapters around the core callback.

The core reports progress through ``orchestrator.run(progress=cb)`` independently
of the UI. Adapters can wrap callbacks with a :class:`ProgressEmitter`:

- ``RedisEmitter`` in apps/api publishes events for the WebSocket relay.

The core has no Redis dependency; it invokes the injected progress callback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

ProgressFn = Callable[[int, int, str], None]
"""Core progress callback: ``(done, total, label)``."""


@dataclass
class TranslationEvent:
    """One progress event.

    ``kind`` identifies the event category. ``label`` carries display text from
    the core ProgressFn callback, and ``payload`` holds optional event details.
    """

    project_id: Optional[str] = None
    kind: str = "progress"  # progress | batch | chapter | term | pipeline | log
    done: int = 0
    total: int = 0
    label: str = ""
    payload: Optional[dict] = None


@runtime_checkable
class ProgressEmitter(Protocol):
    """Interface for publishing progress events."""

    def emit(self, event: TranslationEvent) -> None: ...


def make_progress_fn(
    emitter: ProgressEmitter, project_id: Optional[str] = None, *, kind: str = "progress"
):
    """Wrap a :class:`ProgressEmitter` as a core ProgressFn callback.

    The signature is ``progress(done: int, total: int, label: str) -> None``.
    """

    def fn(done: int, total: int, label: str) -> None:
        emitter.emit(
            TranslationEvent(
                project_id=project_id,
                kind=kind,
                done=done,
                total=total,
                label=label,
            )
        )

    return fn
