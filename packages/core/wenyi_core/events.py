"""Progress event contracts for adapters around the core callback.

The core reports progress through ``orchestrator.run(progress=cb)`` independently
of the UI. Adapters can wrap callbacks with a :class:`ProgressEmitter`:

- ``NullEmitter`` discards events when progress is rendered locally.
- ``RedisEmitter`` in apps/api publishes events for the WebSocket relay.

The core has no Redis dependency; it invokes the injected progress callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


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


class NullEmitter:
    """Discard events when progress is rendered directly by the local client."""

    def emit(self, event: TranslationEvent) -> None:  # noqa: D401
        return None


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
