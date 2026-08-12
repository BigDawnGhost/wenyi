"""Pure EPUB annotation index and context-allocation contracts."""

from __future__ import annotations

import subprocess
import sys

from trans_novel.application.annotations import (
    annotation_contexts_for_segments,
    completed_logical_starts_in_range,
    plan_context_chapter_prefix_update,
)
from trans_novel.ingest.models import Segment


def _annotation_item(
    item_id: str,
    *,
    mode: str,
    start: int,
    end: int,
    target_key: str,
) -> dict[str, object]:
    """Build the parser metadata needed by the allocation tests."""
    return {
        "id": item_id,
        "mode": mode,
        "source_start": start,
        "source_end": end,
        "target_key": target_key,
        "relation": "noteref",
    }


def test_context_allocation_preserves_point_boundaries_ranges_and_deduplication() -> None:
    """A boundary point goes left while a range reaches every intersecting piece."""
    segments = [
        Segment(
            index=0,
            source="abc",
            meta={
                "epub_annotations": {
                    "source_length": 6,
                    "items": [
                        _annotation_item(
                            "point-boundary",
                            mode="point",
                            start=3,
                            end=3,
                            target_key="notes.xhtml#same",
                        ),
                        _annotation_item(
                            "point-duplicate",
                            mode="point",
                            start=1,
                            end=1,
                            target_key="notes.xhtml#same",
                        ),
                        _annotation_item(
                            "range",
                            mode="range",
                            start=2,
                            end=5,
                            target_key="notes.xhtml#range",
                        ),
                    ],
                }
            },
        ),
        Segment(index=1, source="def", cont=True),
    ]
    registry = {
        "contexts": {
            "notes.xhtml#same": {"source_blocks": ["Same note"]},
            "notes.xhtml#range": {"source_blocks": ["Range", "note"]},
        }
    }

    contexts = annotation_contexts_for_segments(segments, registry)

    assert [item["target_key"] for item in contexts[0]] == [
        "notes.xhtml#same",
        "notes.xhtml#range",
    ]
    assert [item["target_key"] for item in contexts[1]] == ["notes.xhtml#range"]
    assert contexts[0][1]["source"] == "Range\n\nnote"


def test_stale_source_length_rejects_the_whole_logical_paragraph() -> None:
    """Persisted offsets must describe the current concatenated source exactly."""
    segments = [
        Segment(
            index=0,
            source="ab",
            meta={
                "epub_annotations": {
                    "source_length": 5,
                    "items": [
                        _annotation_item(
                            "point",
                            mode="point",
                            start=2,
                            end=2,
                            target_key="notes.xhtml#point",
                        )
                    ],
                }
            },
        ),
        Segment(index=1, source="cd", cont=True),
    ]
    registry = {"contexts": {"notes.xhtml#point": {"source_blocks": ["Note"]}}}

    assert annotation_contexts_for_segments(segments, registry) == [[], []]


def test_continuation_group_becomes_eligible_only_in_its_final_batch() -> None:
    """Cross-batch continuation alignment fires once, at the physical tail."""
    segments = [
        Segment(index=0, source="a"),
        Segment(index=1, source="b", cont=True),
        Segment(index=2, source="c", cont=True),
        Segment(index=3, source="d"),
    ]

    assert completed_logical_starts_in_range(segments, 0, 1) == []
    assert completed_logical_starts_in_range(segments, 1, 1) == []
    assert completed_logical_starts_in_range(segments, 2, 1) == [0]
    assert completed_logical_starts_in_range(segments, 0, 4) == [0, 3]


def test_context_plan_replaces_only_available_completed_prefix_tail() -> None:
    """Finalized chapter targets never overwrite older cross-chapter context."""
    segments = [
        Segment(index=0, source="a", target="甲"),
        Segment(index=1, source="b", target="乙"),
    ]

    update = plan_context_chapter_prefix_update(segments, 2, recent_target_count=3)

    assert update is not None
    assert update.retained == 2
    assert update.targets == ("甲", "乙")
    segments[1].target = None
    assert plan_context_chapter_prefix_update(segments, 2, recent_target_count=3) is None


def test_application_annotations_import_has_no_legacy_runtime_dependencies() -> None:
    """The pure algorithms stay reusable by both legacy and graph runtimes."""
    script = """
import sys
import trans_novel.application.annotations

forbidden = (
    "trans_novel.agents.annotation_aligner",
    "trans_novel.config",
    "trans_novel.pipeline.context",
    "trans_novel.pipeline.orchestrator",
    "trans_novel.pipeline.runstore",
    "langgraph",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected annotation dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
