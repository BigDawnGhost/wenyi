"""Offline end-to-end orchestration and resume tests using FakeClient."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_epub, write_sample_txt
from trans_novel.agents.reviewer import ReviewOutputError
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.usage import UsageSample
from trans_novel.pipeline.annotations import AnnotationService
from trans_novel.pipeline.context import RollingContext
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.review_workflow import ReviewService
from trans_novel.pipeline.runstore import (
    STATUS_DONE,
    STATUS_PENDING,
    RunStore,
    slugify,
    source_sha256,
)
from trans_novel.pipeline.translation import TranslationService


def _translated_para_count(calls) -> int:
    """Count source paragraphs sent to translation by their numbered lines."""
    n = 0
    for c in calls:
        if "literary translator" in c["messages"][0]["content"]:
            n += len(re.findall(r"^\[(\d+)\]", c["messages"][-1]["content"], re.MULTILINE))
    return n


def _review_json(user: str, issues: list[dict]) -> str:
    """Build a reviewer response with its completeness receipt."""
    return json.dumps(
        {
            "issues": issues,
            "reviewed_segments": len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE)),
            "complete": True,
        },
        ensure_ascii=False,
    )


def _fix_json(user: str, replacement: str) -> str:
    """Echo identity fields from a fixer request into a complete temporary replacement
    response.
    """

    def field(name: str) -> str:
        match = re.search(rf"^{name}:\s*(.+)$", user, re.MULTILINE)
        if match is None:
            raise AssertionError(f"Fixer prompt missing {name}")
        return match.group(1).strip()

    return json.dumps(
        {
            "segment_ref": field("segment_ref"),
            "before_hash": field("before_hash"),
            "issue_ids": json.loads(field("issue_ids")),
            "replacement": replacement,
            "complete": True,
        },
        ensure_ascii=False,
    )


def _config(state_dir: str):
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
            },
            "segment": {"max_chars_per_batch": 1800},
            "pipeline": {
                "review": True,
                "review_autofix": False,
                "polish": True,
            },
            "paths": {"state_dir": state_dir},
        }
    )


class MeteredFakeClient(FakeClient):
    """Record small usage per offline call to verify review accounting isolation."""

    def complete(
        self,
        messages,
        *,
        tier="strong",
        json_mode=False,
        max_tokens=None,
        stage=None,
    ):
        self.usage.record(
            tier,
            UsageSample(
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
                cache_miss_tokens=5,
            ),
            stage,
        )
        return super().complete(
            messages,
            tier=tier,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )


class TestOrchestrator(unittest.TestCase):
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

    def test_resume_batch_from_continuation_receives_absolute_annotation_slice(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.annotation_alignment = False
            cfg.segment.max_chars_per_batch = 6
            chapter = Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="aa",
                        target="既译",
                        anchor="tn0_0",
                        meta={
                            "epub_annotations": {
                                "version": 1,
                                "source_length": 6,
                                "items": [
                                    {
                                        "id": "second-piece",
                                        "mode": "point",
                                        "source_start": 3,
                                        "source_end": 3,
                                        "target_key": "notes.xhtml#n2",
                                        "relation": "noteref",
                                    },
                                    {
                                        "id": "third-piece",
                                        "mode": "point",
                                        "source_start": 5,
                                        "source_end": 5,
                                        "target_key": "notes.xhtml#n3",
                                        "relation": "noteref",
                                    },
                                ],
                            }
                        },
                    ),
                    Segment(index=1, source="bb", cont=True),
                    Segment(index=2, source="cc", cont=True),
                ],
            )
            registry = {
                "version": 1,
                "contexts": {
                    "notes.xhtml#n2": {"source_blocks": ["Note two"]},
                    "notes.xhtml#n3": {"source_blocks": ["Note three"]},
                },
            }
            store = RunStore(os.path.join(directory, "state", "book"))
            store.save_chapter(chapter)
            store.save_manifest(
                {
                    "title": "Book",
                    "fmt": "epub",
                    "source_lang": "en",
                    "target_lang": "zh",
                    "chapters": [{"index": 0, "title": "", "status": "pending"}],
                }
            )
            glossary = GlossaryStore(store.glossary_path)
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            captured: list[list[list[dict[str, str]]]] = []

            def process(batch, *args, annotation_contexts=None, **kwargs):
                captured.append(annotation_contexts or [])
                return [f"译{segment.source}" for segment in batch]

            try:
                with (
                    patch.object(orch._translation, "process_batch", side_effect=process),
                    patch.object(
                        orch._translation,
                        "extract_batch_glossary",
                        return_value={
                            "inserted": 0,
                            "conflict": 0,
                            "unchanged": 0,
                            "updated": 0,
                        },
                    ),
                    patch.object(
                        orch._runtime.extractor,
                        "extract_and_store",
                        return_value={
                            "inserted": 0,
                            "conflict": 0,
                            "unchanged": 0,
                            "updated": 0,
                        },
                    ),
                ):
                    orch._translation.translate_chapter(
                        0,
                        store,
                        glossary,
                        RollingContext(),
                        "",
                        translation_history={},
                        source_corpus="aabbcc",
                        annotation_context_registry=registry,
                    )
            finally:
                glossary.close()

            self.assertEqual(
                captured,
                [
                    [
                        [{"target_key": "notes.xhtml#n2", "source": "Note two"}],
                        [{"target_key": "notes.xhtml#n3", "source": "Note three"}],
                    ]
                ],
            )

    def test_annotation_alignment_merges_continuations_and_persists_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            cfg.pipeline.annotation_alignment = True

            def handler(messages, tier, json_mode):
                if "align EPUB annotation markers" in messages[0]["content"]:
                    self.assertEqual(tier, "cheap")
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "unit_id": "ch0:tn0_0",
                                    "marked_target": "阿尔法⟪tn0_0_annotation_0⟫ 贝塔",
                                }
                            ],
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
            store = RunStore(os.path.join(directory, "state", "book"))
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
                if "align EPUB annotation markers" in call["messages"][0]["content"]
            ]
            self.assertEqual(len(calls), 1)

    def test_annotation_alignment_uses_formal_target_without_normalizing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = _config(os.path.join(directory, "state"))
            cfg.source_lang = "en"
            requested: list[str] = []

            def handler(messages, tier, json_mode):
                if "align EPUB annotation markers" in messages[0]["content"]:
                    requested.append(messages[-1]["content"])
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "unit_id": "ch0:tn0_0",
                                    "marked_target": "甲,⟪tn0_0_annotation_0⟫乙",
                                }
                            ]
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
            store = RunStore(os.path.join(directory, "state", "book"))
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
                if "align EPUB annotation markers" in messages[0]["content"]:
                    requested.append(messages[-1]["content"])
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "unit_id": "ch0:tn0_0",
                                    "marked_target": "甲⟪tn0_0_annotation_0⟫乙",
                                }
                            ]
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
            store = RunStore(os.path.join(directory, "state", "book"))
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
                if "align EPUB annotation markers" not in messages[0]["content"]:
                    return routing_handler(messages, tier, json_mode)
                user = messages[-1]["content"]
                if '"unit_id": "ch0:a"' in user:
                    unit_id, target, annotation_id = "ch0:a", "甲", "a_note"
                else:
                    unit_id, target, annotation_id = "ch0:b", "乙", "b_note"
                requested_units.append(unit_id)
                return json.dumps(
                    {
                        "items": [
                            {
                                "unit_id": unit_id,
                                "marked_target": f"{target}⟪{annotation_id}⟫",
                            }
                        ]
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
            store = RunStore(os.path.join(directory, "state", "book"))
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))

            orch._annotations.align_annotations_after_batch(0, chapter, 0, 2, store)

            self.assertEqual(requested_units, ["ch0:a", "ch0:b"])
            saved = store.load_chapter(0)
            for segment in saved.segments:
                self.assertEqual(
                    segment.meta["epub_annotations"]["placements"][0]["target_start"],
                    1,
                )

    def test_prepare_retries_after_analysis_failure(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            def fail_analysis(messages, tier, json_mode):
                raise RuntimeError("temporary model failure")

            with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
                Orchestrator(cfg, client=FakeClient(handler=fail_analysis)).prepare(txt)

            run_dirs = [os.path.join(cfg.state_dir, name) for name in os.listdir(cfg.state_dir)]
            self.assertEqual(len(run_dirs), 1)
            self.assertFalse(os.path.isfile(os.path.join(run_dirs[0], "manifest.json")))

            store = Orchestrator(cfg, client=FakeClient(handler=routing_handler)).prepare(txt)
            self.assertTrue(store.exists())
            self.assertTrue(store.load_manifest()["initialized"])
            self.assertIsNotNone(store.load_analysis())

    def test_full_run_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)

            # Every chapter is marked done.
            m = store.load_manifest()
            self.assertEqual(len(m["chapters"]), 2)
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m["chapters"]))

            # Every paragraph has a polished translation.
            ch0 = store.load_chapter(0)
            self.assertTrue(all(s.target for s in ch0.text_segments))
            self.assertTrue(
                all((s.target_before_polish or "").startswith("译") for s in ch0.text_segments)
            )
            self.assertTrue(all((s.target or "").startswith("润") for s in ch0.text_segments))

            # Persist both pre-polish and polished text in chapter JSON, not just in runtime models.
            with open(store.chapter_path(0), encoding="utf-8") as chapter_file:
                chapter_json = json.load(chapter_file)
            self.assertTrue(
                all(
                    "target_before_polish" in segment
                    for segment in chapter_json["segments"]
                    if segment["source"].strip()
                )
            )

            # Verify the extracted character and analysis-seeded character are stored.
            from trans_novel.glossary.store import GlossaryStore

            g = GlossaryStore(store.glossary_path)
            self.assertIsNotNone(g.get_term("綾小路"))
            self.assertIsNotNone(g.get_term("堀北"))
            g.close()

            # Resume completed chapters without further translation calls.
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            orch2.run(txt)  # Resume semantics.
            translate_calls = [
                c for c in client2.calls if "literary translator" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(translate_calls), 0)

    def test_resume_after_partial(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            # Translate chapter zero only.
            store = orch.run(txt, only_chapter=0)
            m = store.load_manifest()
            self.assertEqual(m["chapters"][0]["status"], STATUS_DONE)
            self.assertNotEqual(m["chapters"][1]["status"], STATUS_DONE)

            # Resume should translate only chapter one.
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            chapter_indices = [chapter["index"] for chapter in m["chapters"]]
            expected_total, expected_done = orch2._translation.progress_counts(
                store, chapter_indices
            )
            progress_events: list[tuple[int, int, str]] = []
            store2 = orch2.run(
                txt,
                progress=lambda done, total, label: progress_events.append((done, total, label)),
            )
            m2 = store2.load_manifest()
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m2["chapters"]))
            chapter_label = TranslationService.chapter_progress_label(
                store.load_chapter(1).title, 1
            )
            first_chapter_progress = next(
                event for event in progress_events if event[2] == chapter_label
            )
            self.assertEqual(
                first_chapter_progress,
                (expected_done, expected_total, chapter_label),
            )


class TestSegmentLevelResume(unittest.TestCase):
    def _tr_handler(self, tag):
        """Return a tagged translation handler and delegate other tasks to the shared router."""

        def handler(messages, tier, json_mode):
            if "literary translator" in messages[0]["content"]:
                n = len(re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.MULTILINE))
                return json.dumps(
                    {"translations": [f"{tag}译{i}" for i in range(n)]},
                    ensure_ascii=False,
                )
            return routing_handler(messages, tier, json_mode)

        return handler

    def test_resume_skips_done_segments_keeps_their_text(self):
        """Resume preserves completed targets and translates only unfinished paragraphs."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = (
                8  # Use roughly one paragraph per batch for precise resume assertions.
            )
            cfg.pipeline.polish = False  # Preserve translation tags for assertions; this setting is unrelated to resume behavior.

            # First run: translate chapter zero with the R1 tag.
            c1 = FakeClient(handler=self._tr_handler("R1"))
            store = Orchestrator(cfg, client=c1).run(txt, only_chapter=0)
            ch = store.load_chapter(0)
            self.assertTrue(all(s.target and s.target.startswith("R1") for s in ch.text_segments))

            # Simulate interruption by clearing the last target and resetting the chapter to pending.
            ch.segments[-1].target = ""
            store.save_chapter(ch)
            store.set_chapter_status(0, STATUS_PENDING)

            # Resume with R2 and translate only the single cleared paragraph.
            c2 = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=c2).run(txt, only_chapter=0)
            self.assertEqual(
                _translated_para_count(c2.calls), 1
            )  # Only one paragraph is translated again.

            ch2 = store.load_chapter(0)
            # Saved paragraphs retain R1 without cross-position reuse; the resumed paragraph gets R2.
            first_target = ch2.text_segments[0].target
            last_target = ch2.text_segments[-1].target
            self.assertIsNotNone(first_target)
            self.assertIsNotNone(last_target)
            assert first_target is not None
            assert last_target is not None
            self.assertTrue(first_target.startswith("R1"))
            self.assertTrue(last_target.startswith("R2"))

    def test_resume_splits_mixed_batch_after_budget_change(self):
        """One missing target in a large batch must not overwrite its saved neighboring
        targets.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.polish = False

            first_client = FakeClient(handler=self._tr_handler("R1"))
            store = Orchestrator(cfg, client=first_client).run(txt, only_chapter=0)
            chapter = store.load_chapter(0)
            chapter.text_segments[-1].target = ""
            store.save_chapter(chapter)
            store.set_chapter_status(0, STATUS_PENDING)

            # Changing the budget can still group completed and empty targets together.
            cfg.segment.max_chars_per_batch = 50_000
            second_client = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=second_client).run(txt, only_chapter=0)

            self.assertEqual(_translated_para_count(second_client.calls), 1)
            resumed = store.load_chapter(0).text_segments
            self.assertTrue(
                all((segment.target or "").startswith("R1") for segment in resumed[:-1])
            )
            self.assertTrue((resumed[-1].target or "").startswith("R2"))

    def test_resume_skips_do_not_refresh_term_snapshot_each_batch(self):
        """Pure resume skips avoid glossary refreshes; missing checkpoints extract and refresh
        before translation.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 8

            store = Orchestrator(cfg, client=FakeClient(handler=self._tr_handler("R1"))).run(
                txt, only_chapter=0
            )
            chapter = store.load_chapter(0)
            segments = chapter.text_segments
            self.assertGreater(len(segments), 2)
            # Leave the last paragraph pending and remove the first extraction checkpoint, retaining the others.
            segments[-1].target = ""
            store.save_chapter(chapter)
            store.set_chapter_status(0, STATUS_PENDING)
            first_key = store.batch_glossary_key(0, 1)
            self.assertIn(first_key, store.completed_batch_glossary_keys(0))
            kept_lines: list[str] = []
            for line in Path(store.event_log_path).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if (
                    event.get("event") == "batch_glossary_extracted"
                    and event.get("chapter") == 0
                    and event.get("start_index") == 0
                    and event.get("count") == 1
                ):
                    continue
                kept_lines.append(line)
            Path(store.event_log_path).write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
            store._batch_glossary_event_cache = None
            self.assertNotIn(first_key, store.completed_batch_glossary_keys(0))

            snapshot_calls = {"n": 0}
            extract_batch_calls = {"n": 0}
            orch = Orchestrator(cfg, client=FakeClient(handler=self._tr_handler("R2")))
            real_snapshot = orch._translation.chapter_term_snapshot
            real_extract = orch._translation.extract_batch_glossary

            def counting_snapshot(glossary, text_segs):
                snapshot_calls["n"] += 1
                return real_snapshot(glossary, text_segs)

            def counting_extract(*args, **kwargs):
                extract_batch_calls["n"] += 1
                return real_extract(*args, **kwargs)

            with (
                patch.object(
                    orch._translation,
                    "chapter_term_snapshot",
                    side_effect=counting_snapshot,
                ),
                patch.object(
                    orch._translation,
                    "extract_batch_glossary",
                    side_effect=counting_extract,
                ),
            ):
                orch.run(txt, only_chapter=0)

            # Extract once for the missing checkpoint and once after the final translation; chapter fallback is separate.
            self.assertEqual(extract_batch_calls["n"], 2)
            # Read at chapter start and refresh once before real translation; checkpointed skips do not refresh.
            self.assertEqual(snapshot_calls["n"], 2)
            resumed = store.load_chapter(0).text_segments
            self.assertTrue((resumed[-1].target or "").startswith("R2"))
            self.assertTrue(
                all((segment.target or "").startswith("R1") for segment in resumed[:-1])
            )


class TestBookUnderstanding(unittest.TestCase):
    def _translate_user(self, calls) -> str:
        """Return user text from the last translation call."""
        for c in reversed(calls):
            if "literary translator" in c["messages"][0]["content"]:
                return c["messages"][-1]["content"]
        return ""

    def test_prepass_builds_and_injects(self):
        """Prescan produces and injects chapter digests and the whole-book synopsis."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            # Persist chapter digests in chapter.meta.
            self.assertTrue(store.load_chapter(0).meta.get("source_digest"))
            # Persist the whole-book synopsis in analysis.
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))

            # Translation prompts contain actual synopsis/digest content instead of empty placeholders.
            user = self._translate_user(client.calls)
            self.assertIn("[Whole-book synopsis]", user)
            self.assertIn("[Chapter digest]", user)
            self.assertIn("全书概览", user)  # Fake whole-book synopsis text.
            self.assertIn("本章梗概", user)  # Fake chapter digest text.

    def test_prepare_for_translation_builds_understanding_without_targets(self):
        """Preparation persists analysis, initial terms and synopsis without translating the
        body.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            store = Orchestrator(
                cfg,
                client=client,
            ).prepare_for_translation(txt)

            manifest = store.load_manifest()
            self.assertTrue(store.load_analysis())
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))
            glossary = GlossaryStore(store.glossary_path)
            try:
                self.assertGreater(glossary.stats()["terms"], 0)
            finally:
                glossary.close()
            for item in manifest["chapters"]:
                chapter = store.load_chapter(item["index"])
                self.assertTrue(chapter.meta.get("source_digest"))
                self.assertTrue(all(segment.target is None for segment in chapter.segments))
            translate_calls = [
                call
                for call in client.calls
                if "literary translator" in call["messages"][0]["content"]
            ]
            self.assertEqual(translate_calls, [])

    def test_prescan_parallel(self):
        """Parallel digests are persisted in chapter order and injected into translation
        correctly.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.prescan_concurrency = 3

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            m = store.load_manifest()
            for c in m["chapters"]:
                self.assertTrue(store.load_chapter(c["index"]).meta.get("source_digest"))
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))
            user = self._translate_user(client.calls)
            self.assertIn("[Chapter digest]", user)

    def test_resume_skips_prepass(self):
        """Resume reuses saved digests and synopsis without new prescan calls."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            Orchestrator(cfg, client=FakeClient(handler=routing_handler)).run(txt)

            c2 = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=c2).run(txt)
            prepass = [
                c
                for c in c2.calls
                if "梗概员" in c["messages"][0]["content"]
                or "概览员" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(prepass), 0)

    def test_toggle_off(self):
        """Disabling book_understanding skips prescan and uses empty prompt placeholders."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.book_understanding = False

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            self.assertFalse(store.load_chapter(0).meta.get("source_digest"))
            self.assertFalse((store.load_analysis() or {}).get("book_synopsis"))
            prepass = [
                c
                for c in client.calls
                if "梗概员" in c["messages"][0]["content"]
                or "概览员" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(prepass), 0)


class TestRunSteps(unittest.TestCase):
    def test_subset_only_assemble(self):
        """Assembly-only run_steps is idempotent and makes no translation calls."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run_steps(txt, {"translate"})
            # Assembly only; no new translation calls.
            client2 = FakeClient(handler=routing_handler)
            res = Orchestrator(cfg, client=client2).run_steps(txt, {"assemble"})
            self.assertTrue(res["output"].endswith(".epub"))
            self.assertTrue(os.path.isfile(res["output"]))
            translate_calls = [
                c for c in client2.calls if "literary translator" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(translate_calls), 0)


class TestReviewReporting(unittest.TestCase):
    """Read-only review preserves formal text while persisting results, events and usage."""

    def _handler(self):
        """Report an omission at index zero in every review block and route other calls
        normally.
        """

        def handler(messages, tier, json_mode):
            sys = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in sys:
                return _review_json(
                    user,
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "漏了一句",
                            "suggestion": "补上",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        return handler

    def _run(self, d):
        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        orch = Orchestrator(cfg, client=FakeClient(handler=self._handler()))
        orch.run(txt)
        return orch.run_review(txt)

    @staticmethod
    def _load_internal_issues(result):
        """Read complete round issue records for logical assertions only."""
        return json.loads(
            Path(
                result["review_dir"],
                "rounds/final/unresolved_issues.json",
            ).read_text(encoding="utf-8")
        )

    def test_run_does_not_call_reviewer_even_for_only_chapter(self):
        """Body translation and only_chapter must not implicitly run final review."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            store = Orchestrator(cfg, client=client).run(txt, only_chapter=0)
            Orchestrator(cfg, client=client).run(txt)

            review_calls = [
                call
                for call in client.calls
                if "translation reviewer" in call["messages"][0]["content"]
            ]
            self.assertEqual(review_calls, [])
            self.assertTrue(
                all("review_status" not in chapter for chapter in store.load_manifest()["chapters"])
            )

    def test_review_never_modifies_body_or_translation_state(self):
        """Review creates recommendations without modifying body, manifest, glossary or report."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = MeteredFakeClient(handler=self._handler())
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)
            watched = [
                store.manifest_path,
                store.chapter_path(0),
                store.glossary_path,
                store.report_path,
            ]
            before = {
                path: Path(path).read_bytes() if os.path.exists(path) else None for path in watched
            }
            usage_before = Path(store.usage_path).read_bytes()
            events_before = Path(store.event_log_path).read_bytes()

            result = orch.run_review(txt)
            store = result["store"]
            chapter = store.load_chapter(0)
            self.assertTrue(result["review_issues"])
            self.assertEqual(chapter.meta.get("review_issues", []), [])
            self.assertEqual(
                before,
                {
                    path: Path(path).read_bytes() if os.path.exists(path) else None
                    for path in watched
                },
            )
            self.assertNotEqual(Path(store.usage_path).read_bytes(), usage_before)
            self.assertNotEqual(Path(store.event_log_path).read_bytes(), events_before)
            self.assertTrue(os.path.isfile(os.path.join(result["review_dir"], "result.json")))
            with open(
                os.path.join(result["review_dir"], "usage.json"),
                encoding="utf-8",
            ) as file:
                review_usage = json.load(file)
            self.assertGreater(review_usage["totals"]["calls"], 0)
            self.assertIn("Reviewer", review_usage["by_stage"])
            self.assertNotIn("Translator", review_usage["by_stage"])
            self.assertIn("Reviewer", (store.load_usage() or {})["by_stage"])
            self.assertGreater(
                client.usage_summary()["by_stage"]["Reviewer"]["calls"],
                0,
            )

    def test_review_saves_unified_result_and_round_diagnostics(self):
        with tempfile.TemporaryDirectory() as d:
            result = self._run(d)
            with open(
                os.path.join(
                    result["review_dir"],
                    "rounds/final/initial_issues.json",
                ),
                encoding="utf-8",
            ) as file:
                initial = json.load(file)
            with open(
                os.path.join(result["review_dir"], "result.json"),
                encoding="utf-8",
            ) as file:
                saved = json.load(file)
            self.assertTrue(initial)
            self.assertTrue(saved["issues"])
            self.assertEqual(saved, result["review_result"])
            self.assertEqual(
                set(saved["issues"][0]),
                {"issue_key", "chapter", "index", "type", "detail", "suggestion"},
            )
            self.assertEqual(
                set(os.listdir(result["review_dir"])),
                {
                    "events.jsonl",
                    "result.json",
                    "rounds",
                    "usage.json",
                    "chunks",
                    "checkpoint.json",
                },
            )

    def test_review_only_run_steps_returns_formal_read_only_result(self):
        """Internal review-only steps match the standalone command and preserve formal text."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=MeteredFakeClient(handler=self._handler()))
            store = orch.run(txt)
            manifest_before = Path(store.manifest_path).read_bytes()
            chapter_before = Path(store.chapter_path(0)).read_bytes()

            result = orch.run_steps(txt, {"review"})

            self.assertEqual(Path(store.manifest_path).read_bytes(), manifest_before)
            self.assertEqual(Path(store.chapter_path(0)).read_bytes(), chapter_before)
            self.assertIsNone(result["output"])
            self.assertEqual(result["outputs"], [])
            self.assertTrue(result["review_issues"])
            self.assertTrue(os.path.isfile(os.path.join(result["review_dir"], "result.json")))

    def test_independent_review_does_not_create_report_implicitly(self):
        with tempfile.TemporaryDirectory() as d:
            result = self._run(d)
            self.assertFalse(os.path.exists(result["store"].report_path))

    def test_review_index_mapping(self):
        """Map block-local issue indices correctly to chapter-local positions."""

        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = (
                8  # A review budget of 24 gives each paragraph its own block.
            )
            cfg.pipeline.review_agent_loop = False
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            result = orch.run_review(txt)
            idxs = sorted(
                i["index"]
                for i in result["review_issues"]
                if i.get("chapter") == 0 and i.get("type") == "missing"
            )
            segment_count = len(result["store"].load_chapter(0).text_segments)
            # Local index zero must map to a distinct chapter position for each block.
            self.assertEqual(idxs, list(range(segment_count)))

    def test_review_progress_advances_per_chunk_and_resets_for_blind_round(self):
        """Review advances paragraph progress by block; blind rechecks and clean confirmation
        have separate stages.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = (
                8  # Split each chapter into multiple top-level review blocks.
            )
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 2
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)
            events: list[tuple[int, int, str]] = []

            orch.run_review(
                txt,
                progress=lambda done, total, label: events.append((done, total, label)),
            )

        first = [(done, total) for done, total, label in events if label == "Whole-book review R1"]
        second = [
            (done, total) for done, total, label in events if label == "Blind whole-book review R2"
        ]
        clean = [(done, total) for done, total, label in events if label == "Clean confirmation"]
        self.assertGreater(len(first), 2)
        self.assertGreater(len(second), 2)
        for stage in (first, second):
            self.assertEqual(stage[0][0], 0)
            self.assertEqual(stage[-1][0], stage[-1][1])
            self.assertEqual([done for done, _ in stage], sorted(done for done, _ in stage))
            self.assertTrue(any(0 < done < total for done, total in stage))
        self.assertEqual(clean, [(1, 2), (2, 2)])

    def test_review_accepts_numeric_string_index(self):
        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": "0",
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_agent_loop = False

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            result = orch.run_review(txt)

            issues = result["review_issues"]
            self.assertTrue(issues)
            self.assertEqual(issues[0]["index"], 0)

    def test_review_rejects_invalid_index_instead_of_returning_zero(self):
        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": "unknown",
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_output_retries = 0
            cfg.segment.max_chars_per_batch = 100_000

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            with self.assertRaisesRegex(ReviewOutputError, "invalid_issue_index"):
                orch.run_review(txt)

    def test_review_skips_when_already_completed_with_same_content(self):
        """Automatically reuse completed review when content fingerprints match."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            first = orch.run_review(txt)
            first_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )
            self.assertGreater(first_count, 0)

            second = orch.run_review(txt)
            second_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )
            # Unchanged content skips new LLM calls.
            self.assertEqual(second_count, first_count)
            # Reuse the same result.
            self.assertEqual(first["review_dir"], second["review_dir"])

    def test_review_reruns_when_review_config_changed(self):
        """Changed review configuration invalidates completed results even when content is
        unchanged.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            first = orch.run_review(txt)
            first_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )

            cfg.pipeline.review_agent_max_evidence_rounds = 1  # Change configuration.
            orch2 = Orchestrator(cfg, client=client)
            second = orch2.run_review(txt)
            second_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )
            self.assertGreater(second_count, first_count)  # Run review again.
            self.assertNotEqual(first["review_dir"], second["review_dir"])

    def test_review_running_resume_rejects_config_change(self):
        """Changed configuration must start a new review directory instead of resuming stale
        running state.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            first = orch.run_review(txt)
            review_dir = first["review_dir"]
            result_path = os.path.join(review_dir, "result.json")
            with open(result_path, encoding="utf-8") as f:
                state = json.load(f)
            state["status"] = "running"
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            checkpoint = os.path.join(review_dir, "checkpoint.json")
            if os.path.isfile(checkpoint):
                os.remove(checkpoint)

            cfg.pipeline.review_agent_max_evidence_rounds = 1
            orch2 = Orchestrator(cfg, client=client)
            second = orch2.run_review(txt)
            self.assertNotEqual(first["review_dir"], second["review_dir"])

    def test_try_cached_subchunks_partial_hit_does_not_record(self):
        """A partially cached subtree must not write initial snapshots before its parent
        reruns.
        """
        from trans_novel.review.run_store import ReviewRunStore

        with tempfile.TemporaryDirectory() as d:
            debug = ReviewRunStore(d)
            debug.start(
                reviewed_content_digest="digest",
                metadata={"config": {}, "glossary_fingerprint": "g"},
            )
            # Cache only the left half; the parent and right half remain absent.
            debug.mark_chunk_done(
                "r1-ch0-base0-n2",
                {
                    "issues": [{"index": 0, "type": "mistranslation"}],
                    "initial_issues": [{"index": 0, "type": "mistranslation"}],
                    "dismissed": [],
                },
            )
            pieces = [object(), object(), object(), object()]
            with debug.round_scope(1):
                missed = ReviewService._try_cached_subchunks(0, pieces, debug, "r1-", 0)
            self.assertIsNone(missed)
            initial, dismissed = debug.result_snapshots(1)
            self.assertEqual(initial, [])
            self.assertEqual(dismissed, [])

            debug.mark_chunk_done(
                "r1-ch0-base2-n2",
                {
                    "issues": [{"index": 0, "type": "missing"}],
                    "initial_issues": [{"index": 0, "type": "missing"}],
                    "dismissed": [],
                },
            )
            with debug.round_scope(1):
                hit = ReviewService._try_cached_subchunks(0, pieces, debug, "r1-", 0)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(len(hit), 2)
            initial, _dismissed = debug.result_snapshots(1)
            self.assertEqual(len(initial), 2)

    def test_review_resume_reuses_initial_and_agent_traces(self):
        """After deleting a chunk cache, reuse initial and agent traces without new model
        calls.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=self._handler()))
            orch.run(txt)
            result = orch.run_review(txt)

            # Simulate interruption by resetting a completed review to running and removing its round checkpoint.
            # A real mid-scan interruption has no checkpoint and resumes scanning from round one.
            review_dir = result["review_dir"]
            result_path = os.path.join(review_dir, "result.json")
            with open(result_path, encoding="utf-8") as f:
                state = json.load(f)
            state["status"] = "running"
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            checkpoint = os.path.join(review_dir, "checkpoint.json")
            if os.path.isfile(checkpoint):
                os.remove(checkpoint)

            # Remove the first chunk cache but retain its initial/agent traces.
            chunks_dir = os.path.join(review_dir, "chunks")
            chunk_files = sorted(os.listdir(chunks_dir))
            self.assertTrue(chunk_files)
            removed = os.path.join(chunks_dir, chunk_files[0])
            removed_bytes = Path(removed).read_bytes()
            os.remove(removed)

            meter = MeteredFakeClient(handler=self._handler())
            orch2 = Orchestrator(cfg, client=meter)
            orch2.run_review(txt)

            reused_stages = [
                call["stage"]
                for call in meter.calls
                if call["stage"] in ("Reviewer", "ReviewAgent")
            ]
            self.assertEqual(reused_stages, [])
            self.assertTrue(os.path.isfile(removed))  # Persist the chunk again.
            # Reused output must match the first run byte for byte.
            self.assertEqual(Path(removed).read_bytes(), removed_bytes)
            # The finished-agent fast path does not emit review_agent_resumed in this test;
            # verify that the persisted event stream remains readable.
            with open(os.path.join(review_dir, "events.jsonl"), encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            self.assertTrue(any(e["event"] == "review_leaf_finished" for e in events))

    def test_review_rejects_incomplete_book(self):
        """Standalone final review requires every chapter to be translated."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            store = orch.run(txt, only_chapter=0)

            with self.assertRaisesRegex(ValueError, "requires every chapter to be translated"):
                orch.run_review(txt)

            self.assertFalse(os.path.exists(store.reviews_dir))

    def test_review_without_state_rejects_pdf_before_conversion(self):
        """Reviewing a PDF without state must not convert it or create an empty state
        directory.
        """
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "book.pdf")
            with open(pdf, "wb") as file:
                file.write(b"%PDF-1.4\n")
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)

            with (
                patch("trans_novel.pipeline.preparation.load_document") as loader,
                self.assertRaisesRegex(ValueError, "No translation progress found"),
            ):
                orch.run_review(pdf)

            loader.assert_not_called()
            self.assertEqual(client.calls, [])
            self.assertFalse(os.path.exists(cfg.state_dir))

    def test_review_without_state_does_not_initialize_text_book(self):
        """Without state, ordinary input may be located locally but must not trigger analysis
        or initialization.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            with self.assertRaisesRegex(ValueError, "No translation progress found"):
                Orchestrator(cfg, client=client).run_review(txt)

            self.assertEqual(client.calls, [])
            self.assertFalse(os.path.exists(cfg.state_dir))

    def test_reviewer_failure_keeps_body_and_writes_failed_review_result(self):
        """Service failures preserve body text but persist failed results, events and usage."""

        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                raise RuntimeError("review service unavailable")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            client = MeteredFakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)
            usage_before = Path(store.usage_path).read_bytes()
            events_before = Path(store.event_log_path).read_bytes()

            with self.assertRaisesRegex(RuntimeError, "review service unavailable"):
                orch.run_review(txt)

            review_calls = [
                call
                for call in client.calls
                if "translation reviewer" in call["messages"][0]["content"]
            ]
            # Recover only output protocol errors; splitting must not multiply retries for service failures.
            self.assertEqual(len(review_calls), 1)
            self.assertTrue(
                all("review_status" not in chapter for chapter in store.load_manifest()["chapters"])
            )
            runs = sorted(os.listdir(store.reviews_dir))
            with open(
                os.path.join(store.reviews_dir, runs[-1], "result.json"),
                encoding="utf-8",
            ) as file:
                receipt = json.load(file)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["termination"], "error")
            self.assertEqual(receipt["error"]["type"], "RuntimeError")
            with open(
                os.path.join(store.reviews_dir, runs[-1], "usage.json"),
                encoding="utf-8",
            ) as file:
                review_usage = json.load(file)
            self.assertEqual(review_usage["totals"]["calls"], 1)
            self.assertEqual(review_usage["totals"]["total_tokens"], 8)
            self.assertEqual(review_usage["by_stage"]["Reviewer"]["calls"], 1)
            self.assertNotEqual(Path(store.usage_path).read_bytes(), usage_before)
            self.assertNotEqual(Path(store.event_log_path).read_bytes(), events_before)
            self.assertEqual((store.load_usage() or {})["by_stage"]["Reviewer"]["calls"], 1)
            self.assertEqual(client.usage_summary()["by_stage"]["Reviewer"]["calls"], 1)

    def test_run_steps_records_review_usage_on_success_and_failure(self):
        """Combined workflows persist pre-review and review usage at their respective stage
        boundaries.
        """
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as d:
                txt = os.path.join(d, "novel.txt")
                write_sample_txt(txt)
                cfg = _config(os.path.join(d, "state"))
                cfg.pipeline.review_agent_loop = False
                base_store = Orchestrator(
                    cfg,
                    client=FakeClient(handler=routing_handler),
                ).run(txt)

                def handler(messages, tier, json_mode):
                    if "translation reviewer" in messages[0]["content"]:
                        if fail:
                            raise RuntimeError("review failed")
                        return _review_json(messages[-1]["content"], [])
                    return routing_handler(messages, tier, json_mode)

                client = MeteredFakeClient(handler=handler)
                orch = Orchestrator(cfg, client=client)
                client.usage.record(
                    "cheap",
                    UsageSample(
                        prompt_tokens=11,
                        completion_tokens=7,
                        total_tokens=18,
                        cache_miss_tokens=11,
                    ),
                    "PreReview",
                )

                if fail:
                    with self.assertRaisesRegex(RuntimeError, "review failed"):
                        orch.run_steps(txt, {"review", "report"})
                else:
                    result = orch.run_steps(txt, {"review", "report"})
                    self.assertIsNotNone(result["report"])

                usage = base_store.load_usage()
                self.assertIsNotNone(usage)
                assert usage is not None
                self.assertEqual(usage["by_stage"]["PreReview"]["calls"], 1)
                self.assertIn("Reviewer", usage["by_stage"])
                usage_events = [
                    json.loads(line)
                    for line in Path(base_store.event_log_path)
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if json.loads(line).get("event") == "usage_summary"
                ]
                self.assertTrue(usage_events)
                self.assertIn("Reviewer", json.dumps(usage_events, ensure_ascii=False))

    def test_non_review_run_does_not_report_a_new_review_directory(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)

            reviewed = orch.run_review(txt)
            reported = orch.run_steps(txt, {"report"})

            self.assertIsNotNone(reviewed["review_dir"])
            self.assertIsNone(reported["review_dir"])
            self.assertEqual(
                reported["report"]["review"]["review_id"],
                reviewed["review_result"]["review_id"],
            )

    def test_conflict_arbitration_changes_final_review_suggestions(self):
        """Final arbitration rewrites losing proposals while preserving complete round records."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_fix_loop = False
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)
            progress_events: list[tuple[int, int, str]] = []

            def fake_review(text_segs, terms, *, chapter_index, **kwargs):
                proposed = "绫小路" if chapter_index == 0 else "绫小路君"
                return [
                    {
                        "index": 0,
                        "_chunk_id": f"ch{chapter_index}-chunk",
                        "type": "terminology",
                        "detail": "译名不统一",
                        "suggestion": f"统一为{proposed}",
                        "consistency": {
                            "kind": "term",
                            "subject_source": "綾小路",
                            "proposed_value": proposed,
                        },
                    }
                ]

            def fake_arbitrate(arbiter, conflict):
                issue_ids = [issue["issue_id"] for issue in conflict["issues"]]
                return {
                    "conflict_id": conflict["conflict_id"],
                    "consistency_key": conflict["consistency_key"],
                    "issue_ids": issue_ids,
                    "status": "suggested",
                    "recommended_value": "绫小路",
                    "reason": "沿用首次译名。",
                    "supported_issue_ids": [issue_ids[0]],
                    "rejected_issue_ids": [issue_ids[1]],
                    "evidence_refs": [],
                }

            with (
                patch.object(orch._review, "review_chapter", side_effect=fake_review),
                patch(
                    "trans_novel.pipeline.review_workflow.ReviewConflictArbiter.arbitrate",
                    new=fake_arbitrate,
                ),
            ):
                result = orch.run_review(
                    txt,
                    progress=lambda done, total, label: progress_events.append(
                        (done, total, label)
                    ),
                )

            with open(
                os.path.join(
                    result["review_dir"],
                    "rounds/final/pre_arbitration_issues.json",
                ),
                encoding="utf-8",
            ) as file:
                before = json.load(file)
            final = result["review_result"]["issues"]
            internal_final = self._load_internal_issues(result)
            with open(
                os.path.join(
                    result["review_dir"],
                    "rounds/final/arbitration_superseded_issues.json",
                ),
                encoding="utf-8",
            ) as file:
                superseded = json.load(file)

            self.assertEqual(len(before), 2)
            self.assertEqual(len(final), 2)
            self.assertEqual(len(superseded), 1)
            self.assertEqual(
                {issue["consistency"]["proposed_value"] for issue in internal_final},
                {"绫小路"},
            )
            self.assertEqual(result["review_issues"], final)
            self.assertEqual(
                [
                    (done, total)
                    for done, total, label in progress_events
                    if label == "Conflict arbitration R1"
                ],
                [(0, 1), (1, 1)],
            )

    def test_shadow_fix_is_blindly_rereviewed_with_translation_context(self):
        """Temporary revisions enter the next review without modifying formal state files."""
        review_users: list[str] = []
        fix_users: list[str] = []

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                review_users.append(user)
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "语义不完整",
                            "suggestion": "补全原文信息",
                        },
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "人物译名不统一",
                            "suggestion": "沿用术语表译名",
                        },
                    ]
                    if len(review_users) == 1
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                fix_users.append(user)
                return _fix_json(user, "影子修订译文。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            client = MeteredFakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)
            manifest_before = Path(store.manifest_path).read_bytes()
            chapter_before = Path(store.chapter_path(0)).read_bytes()

            progress_events: list[tuple[int, int, str]] = []
            result = orch.run_review(
                txt,
                progress=lambda done, total, label: progress_events.append((done, total, label)),
            )

            summary = result["review_result"]["summary"]
            patches = json.loads(
                Path(result["review_dir"], "rounds/final/patch-history.json").read_text(
                    encoding="utf-8"
                )
            )
            not_rereported = json.loads(
                Path(
                    result["review_dir"],
                    "rounds/final/not_rereported_patches.json",
                ).read_text(encoding="utf-8")
            )
            fixer_trace_exists = Path(
                result["review_dir"],
                "rounds/001/fixers/ch0-text1.json",
            ).is_file()
            manifest_after = Path(store.manifest_path).read_bytes()
            chapter_after = Path(store.chapter_path(0)).read_bytes()

        self.assertEqual(manifest_after, manifest_before)
        self.assertEqual(chapter_after, chapter_before)
        self.assertEqual(len(fix_users), 1)
        self.assertIn("语义不完整", fix_users[0])
        self.assertIn("人物译名不统一", fix_users[0])
        self.assertIn("Style guide: 克制", fix_users[0])
        self.assertIn("全书概览", fix_users[0])
        self.assertIn("本章梗概", fix_users[0])
        self.assertTrue(
            any("影子修订译文。" in user for user in review_users[2:]),
            "第二轮基础 Reviewer 必须直接读取影子译文",
        )
        self.assertEqual(result["review_issues"], [])
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")
        self.assertEqual(summary["review_round_count"], 3)
        self.assertEqual(summary["fix_round_count"], 1)
        self.assertEqual(summary["not_rereported_patch_count"], 1)
        self.assertEqual(len(patches), 1)
        self.assertEqual(len(patches[0]["issue_ids"]), 2)
        self.assertEqual(patches[0]["status"], "not_rereported")
        self.assertEqual(patches[0]["not_rereported_in_round"], 2)
        self.assertEqual(not_rereported, patches)
        self.assertFalse(Path(result["review_dir"], "verified_patches.json").exists())
        self.assertEqual(
            result["review_changes"],
            [
                {
                    "chapter": 0,
                    "index": 1,
                    "suggested_target": "影子修订译文。",
                    "issue_keys": patches[0]["issue_keys"],
                    "review_result": "not_rereported",
                }
            ],
        )
        self.assertTrue(fixer_trace_exists)
        self.assertEqual(
            [
                (done, total)
                for done, total, label in progress_events
                if label == "Shadow revision R1"
            ],
            [(0, 1), (1, 1)],
        )
        self.assertIn("Blind whole-book review R2", [label for _, _, label in progress_events])

    def test_clean_first_pass_requires_an_independent_confirmation(self):
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            if "translation reviewer" in system:
                review_calls += 1
                return _review_json(messages[-1]["content"], [])
            if "cautious revision editor" in system:
                fix_calls += 1
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 4)  # Two chapters across two blind whole-book review rounds.
        self.assertEqual(fix_calls, 0)
        self.assertEqual(summary["review_round_count"], 2)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")

    def test_last_allowed_fix_still_gets_two_clean_review_passes(self):
        """Allow two complete blind review rounds after the final fix round."""
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": f"第 {call // 2 + 1} 轮仍需调整",
                            "suggestion": "继续改写",
                        }
                    ]
                    if call in {0, 2}
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                fix_calls += 1
                return _fix_json(user, f"影子版本 {fix_calls}。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 8)  # Two chapters across four whole-book review rounds.
        self.assertEqual(fix_calls, 2)
        self.assertEqual(summary["review_round_count"], 4)
        self.assertEqual(summary["fix_round_count"], 2)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")

    def test_clean_pass_before_a_fix_does_not_consume_post_fix_confirmation(self):
        """Clean rounds before a fix must not replace two independent confirmations after the
        patch.
        """
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "第二轮才发现的问题",
                            "suggestion": "修订该段",
                        }
                    ]
                    if call == 2
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                fix_calls += 1
                return _fix_json(user, "迟发现问题的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 1
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 8)  # clean → issue/fix → clean → clean
        self.assertEqual(fix_calls, 1)
        self.assertEqual(summary["review_round_count"], 4)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")

    def test_failed_fixer_issue_survives_when_other_patch_passes_review(self):
        """Fixer failures remain unresolved even if the next reviewer omits them."""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "第一段需修订",
                            "suggestion": "修订第一段",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "第二段需修订",
                            "suggestion": "修订第二段",
                        },
                    ]
                    if call == 0
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "ch0:text0:" in user:
                    return _fix_json(user, "第一段影子修订。")
                return ""
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]
            patches = json.loads(
                Path(result["review_dir"], "rounds/final/patch-history.json").read_text(
                    encoding="utf-8"
                )
            )
            failures = json.loads(
                Path(result["review_dir"], "rounds/final/fix_failures.json").read_text(
                    encoding="utf-8"
                )
            )
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result["review_result"]["termination"], "unresolved_fixes")
        self.assertEqual(summary["blocked_issue_count"], 1)
        self.assertEqual(summary["issue_count"], 1)
        self.assertEqual(len(result["review_issues"]), 1)
        self.assertEqual(result["review_issues"][0]["index"], 1)
        self.assertEqual(internal_issues[0]["fix_failure"]["reason"], "malformed_json")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["status"], "not_rereported")
        self.assertEqual(len(failures), 1)

    def test_blocked_issue_survives_different_patch_on_same_segment(self):
        """A new patch must not clear historical fixer failures that it does not address."""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                if call == 0:
                    issues = [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "用于推动循环的第一段问题",
                            "suggestion": "先修订第一段",
                        },
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "旧术语问题",
                            "suggestion": "沿用既有译名",
                        },
                    ]
                elif call == 2:
                    issues = [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "同段后来发现的新误译问题",
                            "suggestion": "补全该段语义",
                        }
                    ]
                else:
                    issues = []
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "旧术语问题" in user:
                    return ""
                if "用于推动循环的第一段问题" in user:
                    return _fix_json(user, "第一段影子修订。")
                if "同段后来发现的新误译问题" in user:
                    return _fix_json(user, "第二段针对新误译的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(review_calls, 6)  # Three whole-book review rounds, each with two chapters.
        self.assertEqual(result["review_result"]["termination"], "unresolved_fixes")
        self.assertEqual(summary["blocked_issue_count"], 1)
        self.assertEqual(len(result["review_issues"]), 1)
        issue = result["review_issues"][0]
        self.assertEqual((issue["chapter"], issue["index"]), (0, 1))
        self.assertEqual(issue["type"], "terminology")
        self.assertEqual(issue["detail"], "旧术语问题")
        internal_issue = next(
            item for item in internal_issues if item["issue_key"] == issue["issue_key"]
        )
        self.assertEqual(internal_issue["fix_failure"]["reason"], "malformed_json")

    def test_same_segment_same_type_fix_failures_remain_distinct(self):
        """Two independent issues of the same type in one paragraph must not overwrite each
        other's blocked state.
        """
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            if "translation reviewer" in system:
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "人物动作漏译",
                            "suggestion": "补回人物动作",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "时间关系误译",
                            "suggestion": "修正时间关系",
                        },
                    ]
                    if review_calls == 1
                    else []
                )
                return _review_json(messages[-1]["content"], issues)
            if "cautious revision editor" in system:
                return ""
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result["review_result"]["termination"], "no_progress")
        self.assertEqual(summary["blocked_issue_count"], 2)
        self.assertEqual(
            {issue["detail"] for issue in result["review_issues"]},
            {"人物动作漏译", "时间关系误译"},
        )
        self.assertTrue(
            all(issue["fix_failure"]["reason"] == "malformed_json" for issue in internal_issues)
        )

    def test_rereported_blocked_issue_is_deduplicated_across_rounds(self):
        """Repeated logical issues use the latest evidence while retaining prior fixer-failure
        information.
        """
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = []
                if call == 0:
                    issues.append(
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "用于进入下一轮的问题",
                            "suggestion": "先修订第一段",
                        }
                    )
                if call in {0, 2}:
                    issues.append(
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "跨轮重复的术语问题",
                            "suggestion": "统一使用既有译名",
                        }
                    )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "跨轮重复的术语问题" in user:
                    return ""
                if "用于进入下一轮的问题" in user:
                    return _fix_json(user, "用于进入下一轮的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 1
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            internal_issues = self._load_internal_issues(result)

        repeated = [
            issue
            for issue in internal_issues
            if (
                issue.get("chapter"),
                issue.get("index"),
                issue.get("type"),
                issue.get("detail"),
            )
            == (0, 1, "terminology", "跨轮重复的术语问题")
        ]
        self.assertEqual(result["review_result"]["termination"], "max_rounds")
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["review_round"], 2)
        self.assertEqual(repeated[0]["fix_failure"]["reason"], "malformed_json")
        self.assertEqual(repeated[0]["fix_failure"]["review_round"], 1)

    def test_rejected_cycle_patch_does_not_clear_prior_blocked_issue(self):
        """A rejected cyclic overlay must not clear historical blocked issues even when its
        patch applies.
        """
        review_calls = 0
        original_targets: dict[int, str] = {}

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                if call == 0:
                    issues = [
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "循环前已阻塞的术语问题",
                            "suggestion": "使用既有译名",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "先生成版本 B",
                            "suggestion": "改写第二段",
                        },
                    ]
                elif call == 2:
                    issues = [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "同段新发现的问题",
                            "suggestion": "保持第一段原译",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "把第二段恢复原译",
                            "suggestion": "恢复第二段",
                        },
                    ]
                else:
                    issues = []
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "循环前已阻塞的术语问题" in user:
                    return ""
                if "先生成版本 B" in user:
                    return _fix_json(user, "影子版本 B。")
                if "同段新发现的问题" in user:
                    return _fix_json(user, original_targets[0])
                if "把第二段恢复原译" in user:
                    return _fix_json(user, original_targets[1])
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            store = orch.run(txt)
            chapter = store.load_chapter(0)
            original_targets = {
                0: chapter.text_segments[0].target or "",
                1: chapter.text_segments[1].target or "",
            }

            result = orch.run_review(txt)
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result["review_result"]["termination"], "cycle_detected")
        blocked = [
            issue for issue in internal_issues if issue.get("detail") == "循环前已阻塞的术语问题"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["fix_failure"]["reason"], "malformed_json")

    def test_final_summary_includes_blocked_conflicts_and_fallbacks(self):
        """Final summaries must rebuild from all unresolved issues, not only the final clean
        round.
        """

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "cautious revision editor" in system:
                return _fix_json(user, "用于进入盲审轮的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = True
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            def fake_review(
                text_segs,
                terms,
                *,
                chapter_index,
                review_round,
                **kwargs,
            ):
                if review_round != 1:
                    return []
                if chapter_index == 0:
                    return [
                        {
                            "index": 0,
                            "_chunk_id": "conflict-a",
                            "type": "terminology",
                            "detail": "第一种译名",
                            "suggestion": "统一为绫小路",
                            "consistency": {
                                "kind": "term",
                                "subject_source": "綾小路",
                                "proposed_value": "绫小路",
                            },
                        },
                        {
                            "index": 1,
                            "_chunk_id": "fallback-a",
                            "type": "mistranslation",
                            "detail": "Agent 未完成核验",
                            "suggestion": "人工复核",
                            "agent_fallback": True,
                            "fallback_reason": "max_rounds",
                        },
                        {
                            "index": 2,
                            "_chunk_id": "fixable-a",
                            "type": "mistranslation",
                            "detail": "用于进入下一轮的可修问题",
                            "suggestion": "修订该段",
                        },
                    ]
                return [
                    {
                        "index": 0,
                        "_chunk_id": "conflict-b",
                        "type": "terminology",
                        "detail": "第二种译名",
                        "suggestion": "统一为绫小路君",
                        "consistency": {
                            "kind": "term",
                            "subject_source": "綾小路",
                            "proposed_value": "绫小路君",
                        },
                    }
                ]

            with patch.object(orch._review, "review_chapter", side_effect=fake_review):
                result = orch.run_review(txt)

            review_dir = Path(result["review_dir"])
            result_json = json.loads((review_dir / "result.json").read_text(encoding="utf-8"))
            summary = result_json["summary"]
            conflicts = json.loads(
                (review_dir / "rounds/final/conflicts.json").read_text(encoding="utf-8")
            )
            residual = json.loads(
                (review_dir / "rounds/final/residual_conflicts.json").read_text(encoding="utf-8")
            )
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result_json["termination"], "unresolved_fixes")
        self.assertEqual(summary["issue_count"], 3)
        self.assertEqual(summary["blocked_issue_count"], 3)
        self.assertEqual(summary["conflict_count"], 1)
        self.assertEqual(summary["unresolved_conflict_count"], 1)
        self.assertEqual(summary["fallback_agent_count"], 1)
        self.assertEqual(result_json["summary"], summary)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(residual), 1)
        unresolved_ids = {issue["issue_id"] for issue in internal_issues}
        conflict_ids = set(conflicts[0]["issue_ids"])
        residual_ids = set(residual[0]["issue_ids"])
        self.assertEqual(conflict_ids, residual_ids)
        self.assertTrue(conflict_ids <= unresolved_ids)

    def test_shadow_loop_detects_a_b_a_oscillation(self):
        review_calls = 0
        fix_calls = 0
        original_target = ""

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                current = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "仍需调整",
                            "suggestion": "改写",
                        }
                    ]
                    if current % 2 == 0
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                replacement = "影子版本 B。" if fix_calls == 0 else original_target
                fix_calls += 1
                return _fix_json(user, replacement)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            store = orch.run(txt)
            original_target = store.load_chapter(0).text_segments[1].target or ""

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 4)  # Two chapters across two rounds; no third round runs.
        self.assertEqual(fix_calls, 2)
        self.assertEqual(summary["review_round_count"], 2)
        self.assertEqual(result["review_result"]["termination"], "cycle_detected")


class TestStyleAnalysis(unittest.TestCase):
    def test_style_brief_new_fields(self):
        """Render new style dimensions and safely skip fields absent from older analysis."""
        from trans_novel.agents.analyzer import Analyzer
        from trans_novel.llm.providers.fake import FakeClient as FC

        cfg = _config("state")
        ana = Analyzer(FC(), cfg)
        brief = ana.style_brief(
            {
                "genre": "校园",
                "pacing": "短句为主",
                "register": "口语",
                "dialogue_style": "语气词丰富",
                "narration": "第一人称",
            }
        )
        self.assertIn("Pacing: 短句为主", brief)
        self.assertIn("Register: 口语", brief)
        self.assertIn("Dialogue style: 语气词丰富", brief)
        self.assertIn("Narration: 第一人称", brief)
        # Legacy analysis with only older fields.
        old = ana.style_brief({"genre": "校园", "tone": "冷峻"})
        self.assertIn("Genre: 校园", old)
        self.assertNotIn("Pacing:", old)


class TestGlossaryScope(unittest.TestCase):
    def _run_with_terms(self, d, scope):
        from trans_novel.glossary.store import GlossaryStore, GlossaryTerm

        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        cfg.pipeline.glossary_scope = scope

        orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
        store = orch.prepare(txt)
        g = GlossaryStore(store.glossary_path)
        # Include an absent character, an unrelated term and an entity whose alias occurs in the chapter.
        g.upsert_term(GlossaryTerm(source="外部人物X", target="外部译名", type="人物"))
        g.upsert_term(GlossaryTerm(source="無関係用語", target="无关术语", type="术语"))
        g.upsert_term(
            GlossaryTerm(source="ホリキタ", target="堀北译名", aliases=["堀北"], type="术语")
        )
        g.close()

        client = FakeClient(handler=routing_handler)
        Orchestrator(cfg, client=client).run(txt)
        return [
            "\n".join(m["content"] for m in c["messages"])
            for c in client.calls
            if "literary translator" in c["messages"][0]["content"]
        ]

    def test_chapter_scope_prunes(self):
        """Chapter scope excludes absent entries and retains alias matches."""
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d, "chapter")
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertNotIn("外部人物X", p)  # Absent from this chapter; exclude it.
                self.assertNotIn("無関係用語", p)  # Absent from this chapter; exclude it.
                self.assertIn("ホリキタ", p)  # Its alias occurs in body text; retain it.

    def test_full_scope_keeps_all(self):
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d, "full")
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertIn("外部人物X", p)
                self.assertIn("無関係用語", p)
                self.assertIn("ホリキタ", p)

    def test_batch_glossary_refreshes_following_prompts(self):
        """Extract terms after each batch so later prompts immediately receive new forms of
        address.
        """

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "literary translator" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
                return json.dumps(
                    {"translations": ["小夏帆" for _ in range(n)]}, ensure_ascii=False
                )
            if (
                "terminology" in system
                and "extractor" in system
                and "夏帆ちゃん" in user
                and "小夏帆" in user
            ):
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "夏帆ちゃん",
                                "target": "小夏帆",
                                "type": "称谓",
                                "aliases": ["夏帆"],
                                "note": "亲昵称呼",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n「夏帆ちゃん」と母親が言った。\n\n夏帆ちゃんは窓の外を見た。\n"
                )
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 10

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(txt)

            translate_prompts = [
                "\n".join(m["content"] for m in c["messages"])
                for c in client.calls
                if "literary translator" in c["messages"][0]["content"]
            ]
            self.assertGreaterEqual(len(translate_prompts), 3)
            self.assertIn("夏帆ちゃん → 小夏帆", translate_prompts[-1])

    def test_resume_recovers_batch_glossary_checkpoints_from_events(self):
        """Reuse extraction events for old state without duplicate model calls for completed
        batches.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 8

            store = Orchestrator(cfg, client=FakeClient(handler=routing_handler)).run(
                txt, only_chapter=0
            )
            checkpoints = store.completed_batch_glossary_keys(0)
            self.assertGreater(len(checkpoints), 1)

            # Reset a completed chapter to pending; resume must recognize extracted batches from events.
            store.set_chapter_status(0, STATUS_PENDING)

            labels: list[str] = []
            glossary_labels: list[str] = []

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                if "terminology" in system and "extractor" in system:
                    glossary_labels.append(labels[-1])
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(
                txt,
                only_chapter=0,
                progress=lambda _done, _total, label: labels.append(label),
            )

            glossary_calls = [
                call
                for call in client.calls
                if "terminology" in call["messages"][0]["content"]
                and "extractor" in call["messages"][0]["content"]
            ]
            # Skip all saved batches and retain only the chapter-end fallback extraction.
            self.assertEqual(len(glossary_calls), 1)
            self.assertTrue(glossary_labels)
            self.assertTrue(all(label != "Parsing document…" for label in glossary_labels))

    def test_final_glossary_is_available_to_review_prompt(self):
        """Terms discovered in later chapters must be available to final review from the first
        chapter onward.
        """

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "literary translator" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
                return json.dumps(
                    {"translations": ["小夏帆" for _ in range(n)]}, ensure_ascii=False
                )
            if "terminology" in system and "extractor" in system and "後半で" in user:
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "夏帆ちゃん",
                                "target": "小夏帆",
                                "type": "称谓",
                                "aliases": ["夏帆"],
                                "note": "亲昵称呼",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if "terminology" in system and "extractor" in system:
                return json.dumps({"terms": []}, ensure_ascii=False)
            if "terminology consistency aligner" in system:
                self.assertIn("「夏帆ちゃん」と母親が言った。", user)
                self.assertIn('"target": "小夏帆"', user)
                return json.dumps(
                    {"terms": [{"source": "夏帆ちゃん", "target": "小夏帆"}]},
                    ensure_ascii=False,
                )
            if "translation reviewer" in system:
                self.assertIn("夏帆ちゃん → 小夏帆", user)
                return _review_json(user, [])
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n「夏帆ちゃん」と母親が言った。\n\n"
                    "# 第二章\n\n後半で夏帆ちゃんが再び現れた。\n"
                )
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 200

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            orch.run_review(txt)


class TestTierRouting(unittest.TestCase):
    def test_task_tiers(self):
        """Use fast for mechanical tasks, cheap for judgments and strong for translation; bound
        digest output.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)
            orch.run_review(txt)

            expect = {
                "chapter digest writer": "fast",
                "whole-book synopsis writer": "fast",
                "terminology and forms-of-address extractor": "fast",
                "translation reviewer": "cheap",
                "literary translator": "strong",
            }
            seen = set()
            for c in client.calls:
                system = c["messages"][0]["content"]
                for marker, tier in expect.items():
                    if marker in system:
                        self.assertEqual(c["tier"], tier, f"{marker} 应走 {tier} 档")
                        seen.add(marker)
                        if marker == "chapter digest writer":
                            self.assertEqual(c["max_tokens"], 600)
                        if marker == "whole-book synopsis writer":
                            self.assertEqual(c["max_tokens"], 1200)
            self.assertEqual(seen, set(expect), "各类调用都应出现")


class TestProgressLabels(unittest.TestCase):
    def test_progress_label_prefers_real_title(self):
        self.assertEqual(TranslationService.chapter_progress_label("引言", 0), "引言")
        self.assertEqual(TranslationService.chapter_progress_label("第一章", 1), "第一章")
        self.assertEqual(TranslationService.chapter_progress_label("", 1), "Chapter 2")

    def test_progress_covers_preparation_and_output_stages(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            events: list[tuple[int, int, str]] = []
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))

            orch.run_steps(
                txt,
                {"translate", "report", "assemble"},
                progress=lambda done, total, label: events.append((done, total, label)),
            )

            labels = [label for _, _, label in events]
            expected = [
                "Parsing document…",
                "Analyzing book style…",
                "Prescanning chapter digests",
                "Generating whole-book synopsis…",
                "Translating chapter titles…",
                "Translation complete",
                "Generating report…",
                "Assembling translation…",
            ]
            positions = [labels.index(label) for label in expected]
            self.assertEqual(positions, sorted(positions), labels)
            self.assertIn((0, 0, "Generating whole-book synopsis…"), events)


class TestLocateExistingStore(unittest.TestCase):
    def test_epub_locate_uses_peek_title_without_load_document(self):
        """Locate EPUB state through OPF title only, avoiding repeated full-book annotation."""
        with tempfile.TemporaryDirectory() as directory:
            epub = os.path.join(directory, "sample.epub")
            write_sample_epub(epub)
            digest = source_sha256(epub)
            # Use the same slug rule for the sample EPUB's OPF title as preparation does.
            store = RunStore(
                os.path.join(directory, "state", slugify("サンプル小説")),
            )
            store.save_manifest(
                {
                    "title": "サンプル小説",
                    "fmt": "epub",
                    "source_path": epub,
                    "source_sha256": digest,
                    "source_lang": "ja",
                    "target_lang": "zh",
                    "chapters": [],
                }
            )
            cfg = _config(os.path.join(directory, "state"))
            orch = Orchestrator(cfg, client=FakeClient())

            with patch(
                "trans_novel.pipeline.preparation.load_document",
                side_effect=AssertionError("locate 不应调用 load_document"),
            ):
                located = orch._preparation.locate_existing(epub)

            self.assertEqual(located.run_dir, store.run_dir)
            self.assertTrue(located.exists())


if __name__ == "__main__":
    unittest.main()
