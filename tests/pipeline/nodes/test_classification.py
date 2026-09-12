from __future__ import annotations

import tempfile
import unittest

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.chapter_classifier import ChapterObservation
from trans_novel.config import Config
from trans_novel.ingest import (
    Chapter,
    ChapterProcessing,
    Document,
    Segment,
    chapter_source_digest,
)
from trans_novel.pipeline.contracts import NodeRequest
from trans_novel.pipeline.nodes.classification import ensure_chapter_classification
from trans_novel.pipeline.nodes.prepare import AnalyzeNode
from trans_novel.pipeline.state import RunIdentity, RunStore


class _Classifier:
    def __init__(self, kinds: list[str], *, fail_at: int | None = None):
        self.config = Config.from_dict({"llm": fake_llm_dict()})
        self.kinds = list(kinds)
        self.fail_at = fail_at
        self.calls: list[str] = []

    def classify(self, *, chapter_id, title, context, source, hints):
        self.calls.append(source)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("interrupted")
        kind = self.kinds.pop(0)
        return ChapterObservation(chapter_id=chapter_id, kind=kind, reason=f"observed {kind}")


def _document(*chapters: Chapter) -> Document:
    return Document(
        title="Book",
        source_lang="en",
        target_lang="zh",
        fmt="text",
        chapters=list(chapters),
    )


def _stage(store: RunStore, document: Document) -> dict:
    return store.stage_document(
        document,
        RunIdentity(source_bytes_sha256="source", source_lang="en", target_lang="zh"),
    )


class TestClassificationModels(unittest.TestCase):
    def test_processing_is_authoritative_and_load_resets_or_synchronizes_markers(self):
        chapter = Chapter(
            index=0,
            segments=[Segment(index=0, source="source", preserve_source=True)],
        )
        self.assertFalse(chapter.segments[0].preserve_source)

        processing = ChapterProcessing(
            action="preserve",
            review_required=False,
            reason="references only",
            source_sha256=chapter_source_digest(chapter),
            strategy_version="chapter_semantics_v1",
        )
        chapter.set_processing(processing)
        self.assertTrue(chapter.preserve_source)
        self.assertTrue(chapter.segments[0].preserve_source)
        self.assertTrue(Chapter.from_dict(chapter.to_dict()).segments[0].preserve_source)

    def test_source_digest_covers_title_ordered_source_ids_and_semantic_hints(self):
        chapter = Chapter(
            index=0,
            title="Title",
            segments=[Segment(index=4, source="Source")],
            meta={"semantic_hints": ["b", "a", "a"]},
        )
        digest = chapter_source_digest(chapter)
        reordered_hints = chapter.model_copy(deep=True)
        reordered_hints.meta["semantic_hints"] = ["a", "b"]
        self.assertEqual(chapter_source_digest(reordered_hints), digest)
        for changed in (
            chapter.model_copy(update={"title": "Other"}, deep=True),
            Chapter(index=0, title="Title", segments=[Segment(index=5, source="Source")]),
            Chapter(index=0, title="Title", segments=[Segment(index=4, source="Changed")]),
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(chapter_source_digest(changed), digest)


class TestChapterClassificationPersistence(unittest.TestCase):
    def test_all_reference_chunks_preserve_and_persist_both_decisions(self):
        chapter = Chapter(
            index=0,
            title="Appendix",
            segments=[
                Segment(index=0, source="A" * 10_000),
                Segment(index=1, source="B" * 10_000),
            ],
            meta={"semantic_hints": ["xhtml:section:type=bibliography"]},
        )
        document = _document(chapter)
        classifier = _Classifier(["reference_only", "reference_only"])
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, document)
            ensure_chapter_classification(document, store, classifier, manifest)

            self.assertTrue(document.chapters[0].preserve_source)
            self.assertTrue(all(segment.preserve_source for segment in chapter.segments))
            self.assertEqual(store.load_chapter(0).processing.action, "preserve")
            self.assertEqual(manifest["chapters"][0]["processing"]["action"], "preserve")
            self.assertTrue(all(len(source) <= 16_000 for source in classifier.calls))
            self.assertEqual("".join(classifier.calls), "A" * 10_000 + "\n\n" + "B" * 10_000)

    def test_uncertain_observation_translates_with_review_flag(self):
        document = _document(Chapter(index=0, segments=[Segment(index=0, source="Source")]))
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, document)
            ensure_chapter_classification(document, store, _Classifier(["uncertain"]), manifest)

        self.assertEqual(document.chapters[0].processing.action, "translate")
        self.assertTrue(document.chapters[0].processing.review_required)

    def test_partial_cache_resumes_only_remaining_chunks_and_mixture_requires_review(self):
        chapter = Chapter(
            index=0,
            segments=[
                Segment(index=0, source="A" * 8_000),
                Segment(index=1, source="B" * 8_000),
                Segment(index=2, source="C" * 8_000),
            ],
        )
        document = _document(chapter)
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, document)
            interrupted = _Classifier(["reference_only", "translatable"], fail_at=2)
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                ensure_chapter_classification(document, store, interrupted, manifest)
            self.assertIsNone(store.load_chapter(0).processing)

            resumed = _Classifier(["translatable", "translatable"])
            ensure_chapter_classification(document, store, resumed, manifest)
            self.assertEqual(len(resumed.calls), 2)
            self.assertEqual(chapter.processing.action, "translate")
            self.assertTrue(chapter.processing.review_required)

    def test_changed_source_evidence_rejects_incomplete_cache(self):
        document = _document(
            Chapter(
                index=0,
                title="Original",
                segments=[
                    Segment(index=0, source="A" * 8_000),
                    Segment(index=1, source="B" * 8_000),
                ],
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, document)
            with self.assertRaises(RuntimeError):
                ensure_chapter_classification(
                    document, store, _Classifier(["reference_only"], fail_at=2), manifest
                )
            document.chapters[0].title = "Changed"
            with self.assertRaisesRegex(ValueError, "源证据已变化"):
                ensure_chapter_classification(document, store, _Classifier([]), manifest)

    def test_corrupt_required_cache_fails_without_partial_preserve(self):
        document = _document(
            Chapter(
                index=0,
                segments=[
                    Segment(index=0, source="A" * 8_000),
                    Segment(index=1, source="B" * 8_000),
                ],
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, document)
            with self.assertRaises(RuntimeError):
                ensure_chapter_classification(
                    document, store, _Classifier(["reference_only"], fail_at=2), manifest
                )
            store.write_json(store.path_for("chapter_classification.json"), {"broken": True})

            with self.assertRaisesRegex(ValueError, "缓存损坏"):
                ensure_chapter_classification(document, store, _Classifier([]), manifest)
            self.assertIsNone(store.load_chapter(0).processing)

    def test_complete_decision_reuses_without_reading_cache(self):
        original = _document(Chapter(index=0, segments=[Segment(index=0, source="Narrative")]))
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, original)
            ensure_chapter_classification(original, store, _Classifier(["translatable"]), manifest)
            manifest["initialized"] = True
            store.save_manifest(manifest)
            store.write_json(store.path_for("chapter_classification.json"), {"broken": True})

            fresh = _document(Chapter(index=0, segments=[Segment(index=0, source="Narrative")]))
            classifier = _Classifier([])
            ensure_chapter_classification(fresh, store, classifier)
            self.assertEqual(classifier.calls, [])
            self.assertEqual(fresh.chapters[0].processing.action, "translate")

    def test_empty_chapter_never_calls_model(self):
        document = _document(Chapter(index=0, segments=[Segment(index=0, source="  ")]))
        classifier = _Classifier([])
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, document)
            ensure_chapter_classification(document, store, classifier, manifest)
        self.assertEqual(classifier.calls, [])
        self.assertEqual(document.chapters[0].processing.reason, "empty chapter")
        self.assertFalse(document.chapters[0].processing.review_required)

    def test_all_preserve_analysis_skips_analyzer_and_initializes_empty_context(self):
        document = _document(Chapter(index=0, segments=[Segment(index=0, source="Citation.")]))

        class Analyzer:
            def analyze(self, sample):
                raise AssertionError("all-preserve source must not call Analyzer")

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            manifest = _stage(store, document)
            node = AnalyzeNode(
                analyzer=Analyzer(),
                classifier=_Classifier(["reference_only"]),
                config=Config.from_dict({"llm": fake_llm_dict()}),
                doc=document,
                glossary=None,
            )
            node.execute(
                NodeRequest(
                    store=store,
                    node_id="analyze",
                    key="analyze",
                    ci=None,
                    scope="book",
                    input_path="book.txt",
                    artifacts={"prepare": {"manifest": manifest}},
                )
            )

            analysis = store.load_analysis()
            self.assertEqual(analysis["characters"], [])
            self.assertEqual(analysis["terms"], [])
            self.assertEqual(
                {key for key, value in analysis.items() if value == ""},
                {
                    "genre",
                    "tone",
                    "style_guide",
                    "narration",
                    "pacing",
                    "register",
                    "dialogue_style",
                    "rhetoric",
                    "conventions",
                },
            )
            self.assertTrue(store.load_state().initialized)
            self.assertIsNotNone(store.load_context())


if __name__ == "__main__":
    unittest.main()
