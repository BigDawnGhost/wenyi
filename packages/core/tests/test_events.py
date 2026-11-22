"""Progress event contract: optional payloads survive the emitter wrapper."""

from wenyi_core.events import make_progress_fn


class RecordingEmitter:
    """Capture emitted translation events in order."""

    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def test_make_progress_fn_publishes_optional_payload():
    emitter = RecordingEmitter()
    progress = make_progress_fn(emitter, "book", kind="progress")

    progress(1, 4, "Translating")
    progress(2, 4, "Annotating", {"chapter": 3})

    assert emitter.events[0].kind == "progress"
    assert emitter.events[0].payload is None
    assert emitter.events[1].payload == {"chapter": 3}
    assert (emitter.events[1].done, emitter.events[1].total, emitter.events[1].label) == (
        2,
        4,
        "Annotating",
    )
