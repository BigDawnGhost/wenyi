"""Progress event contract for legacy callbacks and structured emitters."""

from wenyi_core import events
from wenyi_core.events import ProgressPayload, TranslationEvent, emit_progress, make_progress_fn


class RecordingEmitter:
    """Capture emitted translation events in order."""

    def __init__(self) -> None:
        self.events: list[TranslationEvent] = []

    def emit(self, event: TranslationEvent) -> None:
        self.events.append(event)


def test_make_progress_fn_publishes_optional_payload():
    emitter = RecordingEmitter()
    progress = make_progress_fn(emitter, "book", kind="progress")

    progress(1, 4, "Translating")
    payload: ProgressPayload = {"chapter": 3}
    emit_progress(progress, 2, 4, "Annotating", payload=payload)

    assert emitter.events[0].kind == "progress"
    assert emitter.events[0].payload is None
    assert emitter.events[1].payload is payload
    assert (emitter.events[1].done, emitter.events[1].total, emitter.events[1].label) == (
        2,
        4,
        "Annotating",
    )


def test_structured_tick_does_not_bind_a_keyword_only_payload():
    seen = []

    def progress(done, total, label, *, payload=None):
        seen.append((done, total, label, payload))

    emit_progress(progress, 2, 4, "Annotating", payload={"chapter": 3})

    assert seen == [(2, 4, "Annotating", None)]


def test_structured_tick_uses_three_arguments_when_signature_is_unavailable(monkeypatch):
    seen = []

    def unavailable(_):
        raise ValueError("no signature")

    monkeypatch.setattr(events, "signature", unavailable)
    emit_progress(lambda *args: seen.append(args), 2, 4, "Annotating", payload={"chapter": 3})

    assert seen == [(2, 4, "Annotating")]
