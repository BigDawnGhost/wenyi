"""Legacy annotation adapter ordering and compatibility contracts."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

from trans_novel.agents.annotation_aligner import AnnotationAlignment, target_digest
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.pipeline.annotations import (
    align_legacy_annotations_after_batch,
    align_legacy_segment_annotation,
)
from trans_novel.pipeline.orchestrator import Orchestrator


class _EventStoreSpy:
    """Record chapter saves and recovery events on one shared timeline."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []

    def save_chapter(self, chapter: Chapter) -> None:
        """Record the durability boundary without touching the filesystem."""
        self.actions.append(("save", chapter))

    def log_event(self, event: str, **attributes: Any) -> None:
        """Record the observable event after or before its associated save."""
        self.actions.append(("event", (event, attributes)))


def _annotated_chapter(*, target: str = "甲,乙") -> Chapter:
    """Build one completed logical paragraph whose punctuation will normalize."""
    return Chapter(
        index=0,
        segments=[
            Segment(
                index=0,
                source="Alpha beta",
                target=target,
                anchor="paragraph-1",
                meta={
                    "epub_annotations": {
                        "items": [{"id": "note-1", "mode": "point"}],
                    }
                },
            )
        ],
    )


def _action_names(store: _EventStoreSpy) -> list[str]:
    """Project the spy timeline to the ordering contract under test."""
    names: list[str] = []
    for kind, payload in store.actions:
        if kind == "event":
            event, _attributes = payload
            names.append(f"event:{event}")
        else:
            names.append(kind)
    return names


def test_batch_adapter_preserves_both_legacy_override_seams() -> None:
    """The facade may still override logical selection and per-unit alignment."""
    chapter = Chapter(
        index=4,
        segments=[
            Segment(index=0, source="a", target="甲"),
            Segment(index=1, source="b", target="乙"),
        ],
    )
    calls: list[tuple[object, ...]] = []

    def completed_starts(segments, start: int, count: int) -> list[int]:
        calls.append(("select", segments, start, count))
        return [1, 0]

    def align_segment(chapter_index, received_chapter, start, store) -> None:
        calls.append(("align", chapter_index, received_chapter, start, store))

    store = object()
    align_legacy_annotations_after_batch(
        4,
        chapter,
        0,
        2,
        store,  # type: ignore[arg-type]
        align_segment=align_segment,
        completed_starts=completed_starts,
    )

    assert calls == [
        ("select", chapter.text_segments, 0, 2),
        ("align", 4, chapter, 1, store),
        ("align", 4, chapter, 0, store),
    ]


def test_orchestrator_facade_routes_through_both_private_override_seams() -> None:
    """Existing tests/integrations can still monkeypatch both private helpers."""
    chapter = Chapter(
        index=4,
        segments=[
            Segment(index=0, source="a", target="甲"),
            Segment(index=1, source="b", target="乙"),
        ],
    )
    orchestrator = object.__new__(Orchestrator)
    completed_starts = Mock(return_value=[1, 0])
    align_segment = Mock()
    orchestrator._completed_logical_starts_in_range = completed_starts
    orchestrator._align_segment_annotation = align_segment
    store = object()

    orchestrator._align_annotations_after_batch(
        4,
        chapter,
        0,
        2,
        store,  # type: ignore[arg-type]
    )

    completed_starts.assert_called_once_with(chapter.text_segments, 0, 2)
    assert align_segment.call_args_list == [
        ((4, chapter, 1, store),),
        ((4, chapter, 0, store),),
    ]


def test_disabled_alignment_logs_policy_decision_before_saving_normalized_target() -> None:
    """Disabled alignment remains observable before punctuation is persisted."""
    chapter = _annotated_chapter()
    store = _EventStoreSpy()
    align_unit = Mock(side_effect=AssertionError("disabled alignment must not call the model"))

    align_legacy_segment_annotation(
        0,
        chapter,
        0,
        store,  # type: ignore[arg-type]
        punctuation_enabled=lambda: True,
        alignment_enabled=lambda: False,
        align_unit=align_unit,
    )

    assert chapter.text_segments[0].target == "甲，乙"
    assert _action_names(store) == ["event:annotation_alignment_skipped", "save"]
    align_unit.assert_not_called()


def test_failed_alignment_saves_normalized_target_before_failure_event() -> None:
    """A recoverable model failure cannot make punctuation changes non-durable."""
    chapter = _annotated_chapter()
    store = _EventStoreSpy()

    def fail_alignment(_unit) -> AnnotationAlignment:
        raise RuntimeError("temporary alignment failure")

    align_legacy_segment_annotation(
        0,
        chapter,
        0,
        store,  # type: ignore[arg-type]
        punctuation_enabled=lambda: True,
        alignment_enabled=lambda: True,
        align_unit=fail_alignment,
    )

    assert chapter.text_segments[0].target == "甲，乙"
    assert _action_names(store) == ["save", "event:annotation_alignment_failed"]


def test_successful_alignment_saves_metadata_before_completion_event() -> None:
    """Completion is announced only after digest and placements are durable."""
    chapter = _annotated_chapter(target="甲乙")
    store = _EventStoreSpy()

    def complete_alignment(unit) -> AnnotationAlignment:
        return AnnotationAlignment(
            unit_id=unit.unit_id,
            target_digest=target_digest(unit.target),
            placements=({"id": "note-1", "target_start": 1, "target_end": 1},),
        )

    align_legacy_segment_annotation(
        0,
        chapter,
        0,
        store,  # type: ignore[arg-type]
        punctuation_enabled=lambda: False,
        alignment_enabled=lambda: True,
        align_unit=complete_alignment,
    )

    metadata = chapter.text_segments[0].meta["epub_annotations"]
    assert metadata["target_digest"] == target_digest("甲乙")
    assert metadata["placements"] == [{"id": "note-1", "target_start": 1, "target_end": 1}]
    assert _action_names(store) == ["save", "event:annotation_alignment_completed"]
