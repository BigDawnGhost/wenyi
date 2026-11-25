"""Offline end-to-end orchestration and resume tests using FakeClient."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import fake_pipeline_config as _config


class TestOrchestratorAnnotation(unittest.TestCase):
    def test_annotation_alignment_merges_continuations_and_persists_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            cfg.pipeline.annotation_alignment = True

            def handler(messages, tier, json_mode):
                if "SEARCH/REPLACE PATCH" in messages[0]["content"]:
                    self.assertEqual(tier, "annotation.align")
                    payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
                    return json.dumps(
                        {
                            "id": payload["id"],
                            "search": "阿尔法 ",
                            "replacement": f"阿尔法{payload['markers'][0]} ",
                        },
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="Alpha ",
                        target="阿尔法 ",
                        anchor="tn0_0",
                        meta={
                            "epub_annotations": {
                                "version": 1,
                                "source_length": len("Alpha beta"),
                                "items": [
                                    {
                                        "id": "tn0_0_annotation_0",
                                        "mode": "point",
                                        "source_start": 5,
                                        "source_end": 5,
                                        "source_text": "",
                                        "marker_text": "1",
                                    }
                                ],
                            }
                        },
                    ),
                    Segment(index=1, source="beta", target="贝塔", cont=True),
                ],
            )
            store = FileStorage(os.path.join(directory, "state", "book"))
            client = FakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)

            orch._annotations.align_annotations_after_batch(
                0,
                chapter,
                0,
                2,
                store,
            )

            saved = store.load_chapter(0)
            metadata = saved.segments[0].meta["epub_annotations"]
            self.assertEqual(metadata["placements"][0]["target_start"], len("阿尔法"))
            self.assertEqual(metadata["placements"][0]["target_end"], len("阿尔法"))
            self.assertEqual(metadata["placements"][0]["status"], "aligned")
            self.assertTrue(metadata["target_digest"])
            calls = [
                call
                for call in client.calls
                if "SEARCH/REPLACE PATCH" in call["messages"][0]["content"]
            ]
            self.assertEqual(len(calls), 1)

    def test_annotation_alignment_uses_formal_target_without_normalizing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            requested: list[str] = []

            def handler(messages, tier, json_mode):
                if "SEARCH/REPLACE PATCH" in messages[0]["content"]:
                    requested.append(messages[-1]["content"])
                    payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
                    return json.dumps(
                        {
                            "id": payload["id"],
                            "search": "甲,乙",
                            "replacement": f"甲,{payload['markers'][0]}乙",
                        },
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="Alpha beta",
                        target="甲,乙",
                        anchor="tn0_0",
                        meta={
                            "epub_annotations": {
                                "version": 1,
                                "source_length": len("Alpha beta"),
                                "items": [
                                    {
                                        "id": "tn0_0_annotation_0",
                                        "mode": "point",
                                        "source_start": 5,
                                        "source_end": 5,
                                        "source_text": "",
                                        "marker_text": "1",
                                    }
                                ],
                            }
                        },
                    )
                ],
            )
            store = FileStorage(os.path.join(directory, "state", "book"))
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))

            orch._annotations.align_annotations_after_batch(0, chapter, 0, 1, store)

            saved = store.load_chapter(0)
            self.assertEqual(saved.segments[0].target, "甲,乙")
            self.assertIn('"immutable_target": "甲,乙"', requested[0])
            self.assertEqual(
                saved.segments[0].meta["epub_annotations"]["placements"][0]["target_start"],
                2,
            )

    def test_annotation_alignment_waits_for_final_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            cfg.pipeline.annotation_alignment = True
            requested: list[str] = []

            def handler(messages, tier, json_mode):
                if "SEARCH/REPLACE PATCH" in messages[0]["content"]:
                    requested.append(messages[-1]["content"])
                    payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
                    return json.dumps(
                        {
                            "id": payload["id"],
                            "search": "甲乙",
                            "replacement": f"甲{payload['markers'][0]}乙",
                        },
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="Alpha ",
                        target="甲",
                        anchor="tn0_0",
                        meta={
                            "epub_annotations": {
                                "version": 1,
                                "source_length": len("Alpha beta"),
                                "items": [
                                    {
                                        "id": "tn0_0_annotation_0",
                                        "mode": "point",
                                        "source_start": 5,
                                        "source_end": 5,
                                        "source_text": "",
                                        "marker_text": "1",
                                    }
                                ],
                            }
                        },
                    ),
                    Segment(index=1, source="beta", target=None, cont=True),
                ],
            )
            store = FileStorage(os.path.join(directory, "state", "book"))
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))

            orch._annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
            self.assertEqual(requested, [])

            chapter.segments[1].target = "乙"
            orch._annotations.align_annotations_after_batch(0, chapter, 1, 1, store)

            self.assertEqual(len(requested), 1)
            self.assertIn('"immutable_target": "甲乙"', requested[0])
            saved = store.load_chapter(0)
            self.assertEqual(
                saved.segments[0].meta["epub_annotations"]["placements"][0]["target_start"],
                1,
            )

    def test_annotation_alignment_processes_multiple_segments_sequentially(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            cfg.pipeline.annotation_alignment = True
            requested_units: list[str] = []

            def annotation_meta(annotation_id: str) -> dict:
                return {
                    "epub_annotations": {
                        "version": 1,
                        "source_length": 1,
                        "items": [
                            {
                                "id": annotation_id,
                                "mode": "point",
                                "source_start": 1,
                                "source_end": 1,
                                "source_text": "",
                                "marker_text": "1",
                            }
                        ],
                    }
                }

            def handler(messages, tier, json_mode):
                if "SEARCH/REPLACE PATCH" not in messages[0]["content"]:
                    return routing_handler(messages, tier, json_mode)
                payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
                unit_id = "ch0:a" if payload["id"] == "a_note" else "ch0:b"
                target = payload["immutable_target"]
                requested_units.append(unit_id)
                return json.dumps(
                    {
                        "id": payload["id"],
                        "search": target,
                        "replacement": target + payload["markers"][0],
                    },
                    ensure_ascii=False,
                )

            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="A",
                        target="甲",
                        anchor="a",
                        meta=annotation_meta("a_note"),
                    ),
                    Segment(
                        index=1,
                        source="B",
                        target="乙",
                        anchor="b",
                        meta=annotation_meta("b_note"),
                    ),
                ],
            )
            store = FileStorage(os.path.join(directory, "state", "book"))
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))

            orch._annotations.align_annotations_after_batch(0, chapter, 0, 2, store)

            self.assertEqual(requested_units, ["ch0:a", "ch0:b"])
            saved = store.load_chapter(0)
            for segment in saved.segments:
                self.assertEqual(
                    segment.meta["epub_annotations"]["placements"][0]["target_start"],
                    1,
                )
