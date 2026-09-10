"""Back-matter policy regressions for the minimal pipeline."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openai import OpenAIError

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.config import Config, PipelineConfig
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.llm.errors import AllModelsFailedError, JSONParseError, ProviderError
from trans_novel.llm.retrying import classify_retry
from trans_novel.pipeline import build_workflow_definition
from trans_novel.pipeline.contracts import GOAL_RUN_ALL, NodeRequest
from trans_novel.pipeline.nodes import TranslateNode, translate_batch
from trans_novel.pipeline.planning import Planner, PrescanInputs, WorkflowPolicy, is_back_matter
from trans_novel.pipeline.state import NODE_TRANSLATE, RollingContext, RunIdentity, RunStore


class _StubTranslator:
    src = "en"
    tgt = "zh"

    def translate_batch(self, sources, **_kwargs):
        return SimpleNamespace(
            translations=tuple(f"译:{source}" for source in sources),
            request_count=1,
        )


class _FailingTranslator(_StubTranslator):
    def __init__(self, error):
        self.error = error

    def translate_batch(self, _sources, **_kwargs):
        raise self.error


class _PermanentProviderError(ProviderError):
    status_code = 401


class TestBackMatterPolicy(unittest.TestCase):
    def _execute(
        self,
        mode: str,
        sources: list[str],
        max_chars: int = 1800,
        translator=None,
    ):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            document = Document(
                title="Book",
                fmt="text",
                source_lang="en",
                target_lang="zh",
                source_path="book.txt",
                chapters=[
                    Chapter(
                        index=0,
                        title="Index",
                        segments=[
                            Segment(index=i, source=source) for i, source in enumerate(sources)
                        ],
                    )
                ],
            )
            state = store.stage_document(
                document,
                RunIdentity(source_bytes_sha256="x", source_lang="en", target_lang="zh"),
            )
            state["initialized"] = True
            store.save_manifest(state)
            config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
            config.pipeline.back_matter = mode
            config.segment.max_chars_per_batch = max_chars
            node = TranslateNode(
                translator=translator or _StubTranslator(),
                extractor=None,
                polisher=None,
                glossary=None,
                config=config,
                style_brief="",
                rolling_context=None,
            )
            progress: list[tuple[int, int, str]] = []
            shared = SimpleNamespace(segments_done=0, segments_total=len(sources))
            node.execute(
                NodeRequest(
                    store=store,
                    node_id=NODE_TRANSLATE,
                    key="translate:0",
                    ci=0,
                    scope="chapter",
                    input_path="",
                    progress=lambda done, total, label: progress.append((done, total, label)),
                    shared=shared,
                    total_chapters=1,
                )
            )
            chapter = store.load_chapter(0)
            saved = store.load_progress(0)
            events = [
                json.loads(line)
                for line in Path(store.event_log_path).read_text(encoding="utf-8").splitlines()
            ]
            return (
                [segment.target for segment in chapter.text_segments],
                progress,
                events,
                store.chapter_status(0),
                saved,
            )

    def test_skip_backmatter_completes_with_source_and_progress(self):
        targets, progress, _events, status, saved = self._execute("skip", ["One.", "Two."])
        self.assertEqual(targets, ["One.", "Two."])
        self.assertEqual(progress, [(2, 2, "第0章 Index")])
        self.assertEqual(status, "done")
        self.assertEqual(saved.back_matter_mode, "skip")
        self.assertEqual(saved.pending_polish, [])
        self.assertEqual(saved.lint_issues, [])

    def test_light_backmatter_advances_each_batch_start_and_progress(self):
        targets, progress, events, status, saved = self._execute(
            "light", ["aaaa", "bbbb", "cccc"], max_chars=5
        )
        self.assertEqual(targets, ["译：aaaa", "译：bbbb", "译：cccc"])
        batches = [
            event
            for event in events
            if event["event"] == "batch_translated" and event.get("back_matter")
        ]
        self.assertEqual([event["start_index"] for event in batches], [0, 1, 2])
        self.assertEqual(
            progress,
            [(1, 3, "第0章 Index"), (2, 3, "第0章 Index"), (3, 3, "第0章 Index")],
        )
        self.assertEqual(status, "done")
        self.assertEqual(saved.back_matter_mode, "light")

    def test_light_backmatter_provider_failures_use_exact_source_fallback(self):
        source = '"Wait...--keep this"'
        for error in (
            WorkflowProtocolError("invalid_response"),
            JSONParseError("invalid json"),
            AllModelsFailedError(()),
            _PermanentProviderError("unauthorized"),
            OpenAIError("provider unavailable"),
        ):
            with self.subTest(error=type(error).__name__):
                targets, _progress, events, _status, _saved = self._execute(
                    "light",
                    [source],
                    translator=_FailingTranslator(error),
                )
                self.assertEqual(targets, [source])
                batch = next(
                    event
                    for event in events
                    if event["event"] == "batch_translated" and event.get("back_matter")
                )
                self.assertEqual(batch["translate_call_count"], 0)

    def test_light_backmatter_connection_error_propagates(self):
        source = '"Wait...--keep this"'
        with self.assertRaisesRegex(ConnectionError, "provider unavailable"):
            self._execute(
                "light",
                [source],
                translator=_FailingTranslator(ConnectionError("provider unavailable")),
            )

    def test_ordinary_translation_provider_failure_uses_exact_source_fallback(self):
        provider_error = _PermanentProviderError("unauthorized")
        self.assertIsNone(classify_retry(provider_error))
        config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
        node = TranslateNode(
            translator=_FailingTranslator(provider_error),
            extractor=None,
            polisher=None,
            glossary=None,
            config=config,
            style_brief="",
            rolling_context=None,
        )
        segments = [Segment(index=0, source='"quoted"')]
        raw, call_count = translate_batch(
            node.translator,
            segments,
            [],
            RollingContext(),
            "",
            chapter_segments=segments,
            start_index=0,
            chapter_title="Chapter",
            n_recent=6,
            single_segment_translation=node.config.pipeline.single_segment_translation,
        )
        self.assertEqual(raw, ['"quoted"'])
        self.assertEqual(call_count, 0)

    def test_ordinary_translation_programming_error_propagates(self):
        config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
        node = TranslateNode(
            translator=_FailingTranslator(RuntimeError("bug")),
            extractor=None,
            polisher=None,
            glossary=None,
            config=config,
            style_brief="",
            rolling_context=None,
        )
        segments = [Segment(index=0, source="text")]
        with self.assertRaisesRegex(RuntimeError, "bug"):
            translate_batch(
                node.translator,
                segments,
                [],
                RollingContext(),
                "",
                chapter_segments=segments,
                start_index=0,
                chapter_title="Chapter",
                n_recent=6,
                single_segment_translation=node.config.pipeline.single_segment_translation,
            )

    def test_economy_defaults_light(self):
        self.assertEqual(PipelineConfig.for_quality("economy").back_matter, "light")

    def test_classifier_only_marks_obvious_back_matter(self):
        self.assertTrue(is_back_matter("索引", index=1, total=2))
        self.assertFalse(is_back_matter("第一章", index=0, total=2))

    def test_light_backmatter_uses_translate_without_polish(self):
        with tempfile.TemporaryDirectory() as d:
            store = RunStore(d)
            doc = Document(
                title="Book",
                fmt="text",
                source_lang="en",
                target_lang="zh",
                source_path="book.txt",
                chapters=[
                    Chapter(index=0, title="Index", segments=[Segment(index=0, source="Note.")]),
                ],
            )
            state = store.stage_document(
                doc, RunIdentity(source_bytes_sha256="x", source_lang="en", target_lang="zh")
            )
            state["initialized"] = True
            store.save_manifest(state)
            plan = Planner(build_workflow_definition()).build_plan(
                goal=GOAL_RUN_ALL,
                store=store,
                policy=WorkflowPolicy(polish=False, back_matter="light"),
                prescan=PrescanInputs(),
            )
            entries = [e for stage in plan.stages for e in stage.entries if e.ci == 0]
            self.assertEqual([e.node_id for e in entries], [NODE_TRANSLATE])
