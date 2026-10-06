"""Annotation service context slicing across logical segment continuations."""

import unittest

from wenyi_core.ingest.models import Segment
from wenyi_core.pipeline.annotations import AnnotationService


class TestAnnotationContexts(unittest.TestCase):
    def test_annotation_contexts_follow_continuation_offsets_and_deduplicate(self):
        segments = [
            Segment(
                index=0,
                source="abc",
                anchor="tn0_0",
                meta={
                    "epub_annotations": {
                        "version": 1,
                        "source_length": 6,
                        "items": [
                            {
                                "id": "point-boundary",
                                "mode": "point",
                                "source_start": 3,
                                "source_end": 3,
                                "source_text": "",
                                "marker_text": "1",
                                "target_key": "notes.xhtml#n1",
                                "relation": "noteref",
                            },
                            {
                                "id": "point-duplicate",
                                "mode": "point",
                                "source_start": 1,
                                "source_end": 1,
                                "source_text": "",
                                "marker_text": "1",
                                "target_key": "notes.xhtml#n1",
                                "relation": "noteref",
                            },
                            {
                                "id": "range-across-pieces",
                                "mode": "range",
                                "source_start": 2,
                                "source_end": 5,
                                "source_text": "cde",
                                "marker_text": "",
                                "target_key": "notes.xhtml#n2",
                                "relation": "noteref",
                            },
                            {
                                "id": "ordinary-link",
                                "mode": "range",
                                "source_start": 0,
                                "source_end": 3,
                                "source_text": "abc",
                                "marker_text": "",
                                "target_key": "chapter.xhtml#part-2",
                                "relation": "internal_link",
                            },
                        ],
                    }
                },
            ),
            Segment(index=1, source="def", cont=True),
        ]
        registry = {
            "version": 1,
            "contexts": {
                "notes.xhtml#n1": {"source_blocks": ["First note."]},
                "notes.xhtml#n2": {"source_blocks": ["Second", "note."]},
                "chapter.xhtml#part-2": {"source_blocks": ["Not a note."]},
            },
        }

        contexts = AnnotationService.annotation_contexts_for_segments(segments, registry)

        self.assertEqual(
            [item["target_key"] for item in contexts[0]],
            ["notes.xhtml#n1", "notes.xhtml#n2"],
        )
        self.assertEqual(
            [item["target_key"] for item in contexts[1]],
            ["notes.xhtml#n2"],
        )
        self.assertEqual(contexts[0][1]["source"], "Second\n\nnote.")

    def test_annotation_context_points_cover_logical_ends_and_reject_stale_length(self):
        metadata = {
            "version": 1,
            "source_length": 4,
            "items": [
                {
                    "id": "at-start",
                    "mode": "point",
                    "source_start": 0,
                    "source_end": 0,
                    "target_key": "notes.xhtml#start",
                    "relation": "noteref",
                },
                {
                    "id": "at-end",
                    "mode": "point",
                    "source_start": 4,
                    "source_end": 4,
                    "target_key": "notes.xhtml#end",
                    "relation": "noteref",
                },
            ],
        }
        segments = [
            Segment(
                index=0,
                source="ab",
                anchor="tn0_0",
                meta={"epub_annotations": metadata},
            ),
            Segment(index=1, source="cd", cont=True),
        ]
        registry = {
            "version": 1,
            "contexts": {
                "notes.xhtml#start": {"source_blocks": ["Start note"]},
                "notes.xhtml#end": {"source_blocks": ["End note"]},
            },
        }

        contexts = AnnotationService.annotation_contexts_for_segments(segments, registry)

        self.assertEqual(
            [[item["target_key"] for item in piece] for piece in contexts],
            [["notes.xhtml#start"], ["notes.xhtml#end"]],
        )
        metadata["source_length"] = 5
        self.assertEqual(
            AnnotationService.annotation_contexts_for_segments(segments, registry),
            [[], []],
        )
