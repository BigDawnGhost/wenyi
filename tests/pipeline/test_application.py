"""Offline regressions for the minimal translation pipeline."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.fixtures.books import write_phase9_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.agents.polisher import Polisher
from trans_novel.config import Config
from trans_novel.epub.slots import EpubSegmentState, EpubTextSlot
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.llm import FakeClient
from trans_novel.llm.errors import ProviderError
from trans_novel.pipeline import Application
from trans_novel.pipeline.contracts import GOAL_RUN_ALL, ExecutionGoal, NodeRequest
from trans_novel.pipeline.nodes import DeterministicQANode, PolishNode, TranslateNode
from trans_novel.pipeline.state import (
    NODE_DETERMINISTIC_QA,
    NODE_REPORT,
    NODE_SUCCEEDED,
    ChapterIndex,
    ChapterProgress,
    NodeState,
    PolishBatch,
    RepairIssue,
    RunIdentity,
    RunState,
    RunStore,
)


def _config(state_dir: str, *, quality: str = "balanced") -> Config:
    config = Config.from_dict({"llm": fake_llm_dict(), "quality": quality})
    config.source_lang = "en"
    config.target_lang = "zh"
    config.state_dir = state_dir
    return config


_MINIMAL_PIPELINE_OPERATIONS = {
    "analyzer.analyze",
    "prescan.term_mine",
    "prescan.name_terms",
    "translate.single",
    "title.translate",
}
_LEGACY_TRANSLATION_OPERATIONS = {"translate.batch"}


def _document() -> Document:
    return Document(
        title="Book",
        fmt="text",
        source_lang="en",
        target_lang="zh",
        source_path="book.txt",
        chapters=[
            Chapter(
                index=0,
                title="Chapter",
                segments=[
                    Segment(index=0, source="A complete sentence."),
                    Segment(index=1, source="An interior segment."),
                ],
            )
        ],
    )


class TestMinimalPipeline(unittest.TestCase):
    def test_balanced_emits_no_polish_or_legacy_operations(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(handler=routing_handler)
            result = Application(_config(d), client=client).run_all(
                _write_source(d), out_format="txt"
            )
            operations = {call["operation"] for call in client.calls}
            self.assertNotIn("polish.segment", operations)
            self.assertTrue(_LEGACY_TRANSLATION_OPERATIONS.isdisjoint(operations))
            self.assertEqual(operations, _MINIMAL_PIPELINE_OPERATIONS)
            state = result["store"].load_state()
            for node_id in (
                "prepare",
                "analyze",
                "mine_terms",
                "name_terms",
                "translate:0",
                "titles",
                "deterministic_qa",
                "repair",
                "report",
                "assemble",
            ):
                self.assertIsNotNone(state.nodes[node_id].started_at)
                self.assertIsNotNone(state.nodes[node_id].finished_at)

    def test_epub_output_preflight_fails_before_any_llm_call(self):
        with tempfile.TemporaryDirectory() as directory:
            source = f"{directory}/broken.epub"
            write_phase9_epub(source)
            with zipfile.ZipFile(source) as archive:
                members = [(info, archive.read(info)) for info in archive.infolist()]
            with zipfile.ZipFile(source, "w") as archive:
                for info, data in members:
                    if info.filename == "OEBPS/text/chapter-1.xhtml":
                        data = data.replace(b"<table>", b"<table><p>bad</p>", 1)
                    archive.writestr(info, data)
            client = FakeClient(handler=lambda *_args: self.fail("preflight must run first"))

            with self.assertRaisesRegex(ValueError, "EPUB 输出预检失败"):
                Application(_config(f"{directory}/state"), client=client).run_all(source)

            self.assertEqual(client.calls, [])

    def test_identical_quality_run_keeps_qa_and_report_fingerprints(self):
        with tempfile.TemporaryDirectory() as d:
            source = _write_source(d)
            app = Application(_config(d), client=FakeClient(handler=routing_handler))
            goal = ExecutionGoal(name="run_all", phases=GOAL_RUN_ALL.phases, out_format="txt")
            doc = _document()
            _, store = app.run_document_goal(doc, source, goal)
            first = store.load_state()
            first_fingerprints = {
                node_id: first.nodes[node_id].input_fingerprint
                for node_id in (NODE_DETERMINISTIC_QA, NODE_REPORT)
            }
            _, store = app.run_document_goal(doc, source, goal)
            second = store.load_state()
            for node_id, fingerprint in first_fingerprints.items():
                self.assertEqual(second.nodes[node_id].status, NODE_SUCCEEDED)
                self.assertEqual(second.nodes[node_id].input_fingerprint, fingerprint)

    def test_translation_fingerprint_uses_persisted_language(self):
        class Translator:
            src = "auto"
            tgt = "zh"

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(d)
            store.save_state(
                RunState(
                    identity=RunIdentity(source_lang="en", target_lang="zh"),
                    chapters=[ChapterIndex(index=0)],
                    progress={0: ChapterProgress()},
                )
            )
            store.save_chapter(
                Chapter(index=0, title="Chapter", segments=[Segment(index=0, source="Hello.")])
            )
            config = _config(d)
            node = TranslateNode.__new__(TranslateNode)
            node.translator = Translator()
            node.config = config
            node.style_brief = ""
            node.frozen_book = None
            node.frozen_preparation = None

            actual = node._fingerprint("Hello.", store, None, 0)
            node.translator.src = "en"
            expected = node._fingerprint("Hello.", store, None, 0)
            self.assertEqual(actual, expected)

    def test_fingerprint_reconciliation_clears_translation_artifacts(self):
        issue = RepairIssue(
            key="issue",
            chapter=0,
            index=0,
            type="too_short",
            attempts=4,
        )
        state = RunState(
            chapters=[ChapterIndex(index=0)],
            progress={
                0: ChapterProgress(
                    pending_polish=[PolishBatch(start=0, count=1)],
                    lint_issues=[{"index": 0}],
                    repair_ledger={"issue": issue},
                )
            },
            nodes={
                "translate:0": NodeState(
                    node_id="translate:0",
                    status=NODE_SUCCEEDED,
                    input_fingerprint="old",
                )
            },
        )
        invalidated = state.reconcile_fingerprints({"translate:0": "new"})
        self.assertIn("translate:0", invalidated)
        self.assertEqual(state.progress[0].pending_polish, [])
        self.assertEqual(state.progress[0].lint_issues, [])
        self.assertEqual(state.progress[0].repair_ledger["issue"].attempts, 4)

    def test_two_segment_light_backmatter_resume_keeps_targets(self):
        with tempfile.TemporaryDirectory() as d:
            source = _write_source(d)
            doc = Document(
                title="Backmatter",
                fmt="text",
                source_lang="en",
                target_lang="zh",
                source_path="book.txt",
                chapters=[
                    Chapter(
                        index=0,
                        title="Index",
                        segments=[
                            Segment(index=0, source="First note."),
                            Segment(index=1, source="Second note."),
                        ],
                    )
                ],
            )
            app = Application(
                _config(d, quality="economy"), client=FakeClient(handler=routing_handler)
            )
            goal = ExecutionGoal(name="run_all", phases=GOAL_RUN_ALL.phases, out_format="txt")
            _, store = app.run_document_goal(doc, source, goal)
            first_targets = [segment.target for segment in store.load_chapter(0).segments]
            with open(store.event_log_path, encoding="utf-8") as stream:
                event_start = stream.read()
            _, store = app.run_document_goal(doc, source, goal)
            second_targets = [segment.target for segment in store.load_chapter(0).segments]
            with open(store.event_log_path, encoding="utf-8") as stream:
                event_end = stream.read()
            self.assertEqual(second_targets, first_targets)
            self.assertNotIn("translate_invalidated", event_end[len(event_start) :])

    def test_quality_adds_only_polish(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(handler=routing_handler)
            Application(_config(d, quality="quality"), client=client).run_all(
                _write_source(d), out_format="txt"
            )
            operations = {call["operation"] for call in client.calls}
            self.assertIn("polish.segment", operations)
            self.assertTrue(_LEGACY_TRANSLATION_OPERATIONS.isdisjoint(operations))
            self.assertEqual(operations - {"polish.segment"}, _MINIMAL_PIPELINE_OPERATIONS)
            self.assertEqual(operations & {"polish.segment"}, {"polish.segment"})

    def test_exhausted_polish_protocol_retries_keep_raw_without_restarting_node(self):
        with tempfile.TemporaryDirectory() as d:
            polish_calls = 0

            def handler(messages, agent, operation, json_mode):
                nonlocal polish_calls
                if operation == "polish.segment":
                    polish_calls += 1
                    return '{"polished": []}'
                return routing_handler(messages, agent, operation, json_mode)

            result = Application(
                _config(d, quality="quality"), client=FakeClient(handler=handler)
            ).run_all(_write_source(d), out_format="txt")

            segments = result["store"].load_chapter(0).text_segments
            self.assertEqual(polish_calls, len(segments) * 3)
            self.assertEqual(result["store"].load_state().nodes["polish:0"].attempts, 1)
            self.assertTrue(all(segment.target for segment in segments))
            self.assertTrue(result["output"])

    def test_exhausted_polish_provider_failure_falls_back_to_raw_batch(self):
        with tempfile.TemporaryDirectory() as d:

            def handler(messages, agent, operation, json_mode):
                if operation == "polish.segment":
                    raise ProviderError("editor unavailable")
                return routing_handler(messages, agent, operation, json_mode)

            result = Application(
                _config(d, quality="quality"), client=FakeClient(handler=handler)
            ).run_all(_write_source(d), out_format="txt")

            store = result["store"]
            self.assertEqual(store.load_state().nodes["polish:0"].status, "succeeded")
            self.assertTrue(all(segment.target for segment in store.load_chapter(0).text_segments))
            with open(store.event_log_path, encoding="utf-8") as stream:
                self.assertIn('"event": "polish_batch_fallback"', stream.read())

    def test_quality_epub_machine_literal_preserves_exact_slots(self):
        with tempfile.TemporaryDirectory() as d:
            config = _config(d, quality="quality")
            client = FakeClient(
                handler=lambda *args: self.fail("machine literals must not call LLM")
            )
            polisher = Polisher(client, config)
            literal = "{var=a--b}"
            state = EpubSegmentState(
                resource_href="chapter.xhtml",
                resource_sha256="source",
                block_path=(0,),
                block_fingerprint="block",
                parse_mode="xml",
                slots=[
                    EpubTextSlot(
                        id="slot",
                        field="text",
                        source_value=f" {literal} ",
                        target_value=f" {literal} ",
                    )
                ],
                slot_contract_sha256="contract",
            )
            segment = Segment(index=0, source=literal, target=literal, epub_state=state)
            chapter = Chapter(index=0, title="Chapter", segments=[segment])
            progress = ChapterProgress(pending_polish=[PolishBatch(start=0, count=1)])

            class Store:
                def save_chapter(self, _chapter):
                    return None

                def save_progress(self, _ci, _progress):
                    return None

                def log_event(self, _event, **_data):
                    return None

            node = PolishNode(
                polisher=polisher,
                extractor=object(),
                glossary=object(),
                config=config,
                style_brief="",
            )
            with (
                patch("trans_novel.pipeline.nodes.polish.begin_polish"),
                patch("trans_novel.pipeline.nodes.polish.clear"),
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                node._drain_chapter_polish(
                    chapter,
                    progress,
                    [segment],
                    {},
                    executor,
                    "",
                    [],
                    Store(),
                    0,
                )

            self.assertEqual(segment.epub_state.slots[0].target_value, f" {literal} ")
            self.assertEqual(segment.target, literal)
            self.assertEqual(client.calls, [])

    def test_titles_are_translated_one_per_call(self):
        with tempfile.TemporaryDirectory() as d:
            doc = _document()
            doc.chapters.append(
                Chapter(index=1, title="Second", segments=[Segment(index=0, source="More text.")])
            )
            source = _write_source(d)
            client = FakeClient(handler=routing_handler)
            goal = ExecutionGoal(name="run_all", phases=GOAL_RUN_ALL.phases, out_format="txt")

            Application(_config(d), client=client).run_document_goal(doc, source, goal)

            title_calls = [call for call in client.calls if call["operation"] == "title.translate"]
            self.assertEqual(len(title_calls), 2)
            self.assertTrue(
                all("[1]" not in call["messages"][-1]["content"] for call in title_calls)
            )

    def test_polish_disabled_body_chapter_reaches_done(self):
        with tempfile.TemporaryDirectory() as d:
            store = Application(_config(d), client=FakeClient(handler=routing_handler)).run(
                _write_source(d)
            )
            self.assertEqual(store.load_progress(0).status, "done")
            self.assertTrue(store.load_chapter(0).segments[0].target.startswith("译"))

    def test_term_mining_and_naming_have_no_summary_operation(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient(handler=routing_handler)
            Application(_config(d), client=client).prepare_for_translation(_write_source(d))
            operations = {call["operation"] for call in client.calls}
            self.assertTrue({"prescan.term_mine", "prescan.name_terms"} <= operations)
            self.assertTrue(
                all(op.startswith(("language.", "analyzer.", "prescan.")) for op in operations)
            )

    def test_deterministic_qa_scans_interior_without_llm_or_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            store = RunStore(d)
            doc = _document()
            state = store.stage_document(
                doc, RunIdentity(source_bytes_sha256="x", source_lang="en", target_lang="zh")
            )
            state["initialized"] = True
            store.save_manifest(state)
            progress = store.load_progress(0)
            progress.status = "done"
            store.save_progress(0, progress)
            chapter = store.load_chapter(0)
            chapter.segments[0].target = "A complete sentence."
            chapter.segments[1].target = "wrong"
            store.save_chapter(chapter)
            before = [s.target for s in chapter.segments]
            glossary = GlossaryStore(store.glossary_path)
            glossary.upsert_term(GlossaryTerm(source="interior", target="内部", locked=True))
            try:
                client = FakeClient(handler=routing_handler)
                node = DeterministicQANode(glossary=glossary)
                outcome = node.execute(
                    NodeRequest(
                        store=store,
                        node_id=NODE_DETERMINISTIC_QA,
                        key=NODE_DETERMINISTIC_QA,
                        ci=None,
                        scope="book",
                        input_path="book.txt",
                    )
                )
                self.assertTrue(
                    any(
                        issue["chapter"] == 0 and issue["index"] == 1
                        for issue in outcome.artifacts["issues"]
                    )
                )
                self.assertEqual([s.target for s in store.load_chapter(0).segments], before)
                self.assertEqual(client.calls, [])
            finally:
                glossary.close()


def _write_source(directory: str) -> str:
    path = f"{directory}/book.txt"
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("Chapter\n\nA complete sentence.\n\nAn interior segment.")
    return path
