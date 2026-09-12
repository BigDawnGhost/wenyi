"""源文窗口、批内历史与章节恢复的离线行为回归。"""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.polisher import Polisher
from trans_novel.agents.translator import Translator
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.models import KIND_HEADING, Chapter, Document, Segment
from trans_novel.llm import FakeClient
from trans_novel.pipeline.contracts import NodeRequest
from trans_novel.pipeline.nodes import PolishNode, TranslateNode
from trans_novel.pipeline.nodes.common import seed_chapter_context, source_context_before
from trans_novel.pipeline.nodes.translation_batch import translate_batch
from trans_novel.pipeline.state import (
    STATUS_DONE,
    PolishBatch,
    RollingContext,
    RunIdentity,
    RunStore,
)


def _segments(*sources):
    return [Segment(index=index, source=source) for index, source in enumerate(sources)]


def _translator(handler, *, quality="balanced"):
    config = Config.from_dict({"llm": fake_llm_dict(), "quality": quality})
    config.source_lang = "en"
    config.pipeline.protocol_retry_limit = 1
    return Translator(FakeClient(handler=handler), config)


class TestSourceWindow(unittest.TestCase):
    def test_only_previous_two_paragraph_tails_are_visible(self):
        segments = _segments(
            "OLD_SOURCE", "TRIMMED_PREFIX" + "x" * 1200, "RECENT", "CURRENT", "FUTURE"
        )
        self.assertEqual(source_context_before(segments, 0), "")
        self.assertEqual(source_context_before(segments, 1), "[Source paragraph 0]\nOLD_SOURCE")
        self.assertEqual(
            source_context_before(segments, 3),
            "[Source paragraph 1]\n[Earlier source truncated]\n"
            + "x" * 1200
            + "\n\n[Source paragraph 2]\nRECENT",
        )
        self.assertEqual(source_context_before(_segments("NEW_CHAPTER"), 0), "")


class TestBatchHistory(unittest.TestCase):
    def test_each_request_uses_latest_local_target_and_preceding_source_only(self):
        responses = iter(["译甲", "译乙"])
        translator = _translator(lambda *args: next(responses))
        segments = _segments("PRECEDING_SOURCE", "CURRENT_ALPHA", "CURRENT_BETA", "FUTURE_SOURCE")
        context = RollingContext(recent_targets=["STALE_TARGET", "RECENT_TARGET"])
        targets, count = translate_batch(
            translator,
            segments[1:3],
            [],
            context,
            "STYLE_MARKER",
            chapter_segments=segments,
            start_index=1,
            chapter_title="ORIGINAL_CHAPTER",
            n_recent=1,
        )
        self.assertEqual((targets, count), (["译甲", "译乙"], 2))
        first, second = [call["messages"][-1]["content"] for call in translator.client.calls]
        self.assertIn("RECENT_TARGET", first)
        self.assertNotIn("STALE_TARGET", first)
        self.assertIn("译甲", second)
        self.assertNotIn("RECENT_TARGET", second)
        self.assertIn("PRECEDING_SOURCE", first)
        self.assertNotIn("CURRENT_BETA", first)
        self.assertIn("CURRENT_ALPHA", second)
        for prompt in (first, second):
            self.assertIn("ORIGINAL_CHAPTER", prompt)
            self.assertIn("STYLE_MARKER", prompt)
            self.assertNotIn("FUTURE_SOURCE", prompt)
        self.assertEqual(context.recent_targets, ["STALE_TARGET", "RECENT_TARGET"])

    def test_heading_is_isolated_but_accepted_target_enters_following_history(self):
        translator = _translator(
            lambda m, a, o, j: "标题译文" if o == "translate.heading" else "正文译文"
        )
        segments = _segments("Original Heading", "Body paragraph")
        segments[0].kind = KIND_HEADING
        translate_batch(
            translator,
            segments,
            [],
            RollingContext(recent_targets=["OLD_TARGET"]),
            "STYLE_MARKER",
            chapter_segments=segments,
            start_index=0,
            chapter_title="CHAPTER_MARKER",
            n_recent=1,
        )
        heading, body = [call["messages"][-1]["content"] for call in translator.client.calls]
        self.assertNotIn("CHAPTER_MARKER", heading)
        self.assertNotIn("STYLE_MARKER", heading)
        self.assertNotIn("OLD_TARGET", heading)
        self.assertIn("标题译文", body)
        self.assertNotIn("OLD_TARGET", body)

    def test_later_protocol_failure_discards_partial_batch_history(self):
        responses = iter(["成功译文", "", "", "", ""])
        translator = _translator(lambda *args: next(responses))
        context = RollingContext(recent_targets=["COMMITTED_TARGET"])
        segments = _segments("Alpha", "Beta")
        targets, count = translate_batch(
            translator,
            segments,
            [],
            context,
            "",
            chapter_segments=segments,
            start_index=0,
            chapter_title="Chapter",
            n_recent=2,
        )
        self.assertEqual((targets, count), (["Alpha", "Beta"], 0))
        self.assertEqual(context.recent_targets, ["COMMITTED_TARGET"])
        self.assertIn("成功译文", translator.client.calls[1]["messages"][-1]["content"])

    def test_economy_fallback_retains_preceding_source_and_chapter(self):
        translator = _translator(
            lambda m, a, o, j: json.dumps({"translations": []}) if j else "正文译文",
            quality="economy",
        )
        segments = _segments("EARLIER_SOURCE", "Current paragraph", "FUTURE_SOURCE")
        translate_batch(
            translator,
            segments[1:2],
            [],
            RollingContext(recent_targets=["RECENT_TARGET"]),
            "STYLE_MARKER",
            chapter_segments=segments,
            start_index=1,
            chapter_title="ORIGINAL_CHAPTER",
            n_recent=1,
            single_segment_translation=False,
        )
        self.assertEqual(len(translator.client.calls), 3)
        for call in translator.client.calls:
            prompt = call["messages"][-1]["content"]
            self.assertIn("EARLIER_SOURCE", prompt)
            self.assertIn("ORIGINAL_CHAPTER", prompt)
            self.assertIn("RECENT_TARGET", prompt)
            self.assertIn("STYLE_MARKER", prompt)
            self.assertNotIn("FUTURE_SOURCE", prompt)


class TestChapterHistoryRecovery(unittest.TestCase):
    def test_seed_uses_committed_source_order_and_stops_at_incomplete_chapter(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            chapters = [
                Chapter(index=index, title=f"Chapter {index}", segments=_segments("Alpha", "Beta"))
                for index in (5, 2, 9, 1)
            ]
            for chapter in chapters:
                for segment in chapter.segments:
                    segment.assign_translation(f"TARGET_{chapter.index}_{segment.index}")
            state = store.stage_document(
                Document(
                    title="Book", fmt="text", source_lang="en", target_lang="zh", chapters=chapters
                ),
                RunIdentity(source_bytes_sha256="x", source_lang="en", target_lang="zh"),
            )
            store.save_manifest(state)
            for index in (5, 2, 1):
                store.set_chapter_status(index, STATUS_DONE)
            context = RollingContext(recent_targets=["CACHED_FUTURE"], max_recent_keep=3)
            seed_chapter_context(context, store, 9)
            self.assertEqual(context.recent_targets, ["TARGET_5_1", "TARGET_2_0", "TARGET_2_1"])
            seed_chapter_context(context, store, 1)
            self.assertEqual(context.recent_targets, [])
            seed_chapter_context(context, store, 5)
            self.assertEqual(context.recent_targets, [])

    def test_prior_chapter_gap_never_admits_later_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            previous = Chapter(
                index=0, title="Previous", segments=_segments("Alpha", "Beta", "Gamma")
            )
            previous.segments[0].assign_translation("COMMITTED_PREFIX")
            previous.segments[2].assign_translation("AFTER_GAP")
            state = store.stage_document(
                Document(
                    title="Book",
                    fmt="text",
                    source_lang="en",
                    target_lang="zh",
                    chapters=[previous, Chapter(index=1, title="Current")],
                ),
                RunIdentity(source_bytes_sha256="x", source_lang="en", target_lang="zh"),
            )
            store.save_manifest(state)
            store.set_chapter_status(0, STATUS_DONE)
            context = RollingContext(recent_targets=["UNTRUSTED_CACHE"])
            seed_chapter_context(context, store, 1)
            self.assertEqual(context.recent_targets, ["COMMITTED_PREFIX"])


class TestPolishRecoveryContext(unittest.TestCase):
    def test_submitted_and_rebuilt_pending_requests_have_identical_source_windows(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            translator = _translator(lambda *args: "译文内容")
            config = translator.config
            config.pipeline.polish = True
            config.pipeline.inflight_glossary = False
            config.segment.max_chars_per_batch = 20
            glossary = GlossaryStore(f"{directory}/glossary.db")
            self.addCleanup(glossary.close)
            client = FakeClient(
                handler=lambda *args: json.dumps({"polished": [{"id": 0, "text": "译文内容"}]})
            )
            polisher = Polisher(client, config)
            segments = _segments("SOURCE_ALPHA", "SOURCE_BETA", "SOURCE_GAMMA", "SOURCE_DELTA")
            chapter = Chapter(index=0, title="ORIGINAL_CHAPTER", segments=segments)
            store = RunStore(directory)
            state = store.stage_document(
                Document(
                    title="Book", fmt="text", source_lang="en", target_lang="zh", chapters=[chapter]
                ),
                RunIdentity(source_bytes_sha256="x", source_lang="en", target_lang="zh"),
            )
            store.save_manifest(state)
            shared = SimpleNamespace(segments_done=0, segments_total=4, polish_futures={})
            request = NodeRequest(
                store=store,
                node_id="translate",
                key="translate:0",
                ci=0,
                scope="chapter",
                input_path="",
                executor=executor,
                shared=shared,
                total_chapters=1,
            )
            TranslateNode(
                translator=translator,
                extractor=None,
                polisher=polisher,
                glossary=glossary,
                config=config,
                style_brief="STYLE_MARKER",
                rolling_context=RollingContext(),
            ).execute(request)
            for future, _request_terms in shared.polish_futures.values():
                future.result()
            submitted = [call["messages"] for call in client.calls]
            self.assertEqual(len(submitted), 4)
            shared.polish_futures.clear()
            client.calls.clear()
            PolishNode(
                polisher=polisher,
                extractor=None,
                glossary=glossary,
                config=config,
                style_brief="STYLE_MARKER",
            ).execute(request)
            rebuilt = [call["messages"] for call in client.calls]
            self.assertEqual(rebuilt, submitted)
            first, last = submitted[0][-1]["content"], submitted[-1][-1]["content"]
            self.assertNotIn("SOURCE_BETA", first)
            self.assertNotIn("SOURCE_DELTA", first)
            self.assertNotIn("SOURCE_ALPHA", last)
            self.assertIn("SOURCE_BETA", last)
            self.assertIn("SOURCE_GAMMA", last)
            self.assertIn("ORIGINAL_CHAPTER", last)
            self.assertEqual(store.load_progress(0).pending_polish, [])

    def test_legacy_pending_batch_recovers_without_cached_future(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            config = _translator(lambda *args: "unused").config
            config.pipeline.polish = True
            config.pipeline.inflight_glossary = False
            glossary = GlossaryStore(f"{directory}/glossary.db")
            self.addCleanup(glossary.close)
            segments = _segments("SOURCE_ALPHA", "SOURCE_BETA")
            for segment, target in zip(segments, ("译文甲。", "译文乙。"), strict=True):
                segment.assign_translation(target)
            chapter = Chapter(index=0, title="Chapter", segments=segments)
            store = RunStore(directory)
            state = store.stage_document(
                Document(
                    title="Book", fmt="text", source_lang="en", target_lang="zh", chapters=[chapter]
                ),
                RunIdentity(source_bytes_sha256="x", source_lang="en", target_lang="zh"),
            )
            store.save_manifest(state)
            progress = store.load_progress(0)
            progress.pending_polish = [PolishBatch(start=0, count=2)]
            store.save_progress(0, progress)
            client = FakeClient(
                handler=lambda *args: json.dumps(
                    {
                        "polished": [
                            {"id": 0, "text": "译文甲。"},
                            {"id": 1, "text": "译文乙。"},
                        ]
                    }
                )
            )
            shared = SimpleNamespace(polish_futures={})
            request = NodeRequest(
                store=store,
                node_id="polish",
                key="polish:0",
                ci=0,
                scope="chapter",
                input_path="",
                executor=executor,
                shared=shared,
                total_chapters=1,
            )

            PolishNode(
                polisher=Polisher(client, config),
                extractor=None,
                glossary=glossary,
                config=config,
                style_brief="",
            ).execute(request)

            self.assertEqual([call["operation"] for call in client.calls], ["polish.batch"])
            self.assertEqual(store.load_progress(0).pending_polish, [])
            with open(store.event_log_path, encoding="utf-8") as stream:
                self.assertIn('"polish_strategy": "checkpoint_batch_v1"', stream.read())
