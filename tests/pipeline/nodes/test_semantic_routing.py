"""章节语义路由回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryTerm
from trans_novel.ingest.models import (
    Chapter,
    ChapterProcessing,
    Document,
    Segment,
    chapter_source_digest,
)
from trans_novel.pipeline import build_workflow_definition
from trans_novel.pipeline.contracts import GOAL_RUN_ALL, NodeRequest
from trans_novel.pipeline.execution import assemble_readiness_problems
from trans_novel.pipeline.nodes.finish import TitlesNode
from trans_novel.pipeline.nodes.repair import RepairNode
from trans_novel.pipeline.nodes.translate import TranslateNode
from trans_novel.pipeline.planning import Planner, PrescanInputs, WorkflowPolicy, sample_text
from trans_novel.pipeline.state import (
    NODE_ANALYZE,
    NODE_DETERMINISTIC_QA,
    NODE_POLISH,
    NODE_PREPARE,
    NODE_REPAIR,
    NODE_REPORT,
    NODE_TITLES,
    NODE_TRANSLATE,
    IdentityMismatchError,
    NodeState,
    RollingContext,
    RunIdentity,
    RunStore,
)


def _classified_chapter(
    index: int,
    title: str,
    source: str,
    *,
    action: str,
    review_required: bool = False,
    toc_entry_id: str | None = None,
) -> Chapter:
    chapter = Chapter(
        index=index,
        title=title,
        segments=[Segment(index=0, source=source)],
        meta={"toc_entry_id": toc_entry_id} if toc_entry_id else {},
    )
    chapter.set_processing(
        ChapterProcessing(
            action=action,
            review_required=review_required,
            reason="reference list" if action == "preserve" else "prose",
            source_sha256=chapter_source_digest(chapter),
            strategy_version="chapter_semantics_v1",
        )
    )
    return chapter


def _store(directory: str, chapters: list[Chapter], *, toc_entries=None) -> RunStore:
    store = RunStore(directory)
    document = Document(
        title="Book",
        fmt="text",
        source_lang="en",
        target_lang="zh",
        source_path="book.txt",
        chapters=chapters,
        meta={"toc_entries": toc_entries or []},
    )
    state = store.stage_document(
        document,
        RunIdentity(source_bytes_sha256="source", source_lang="en", target_lang="zh"),
    )
    state["initialized"] = True
    store.save_manifest(state)
    return store


class _NeverTranslator:
    src = "en"

    def translate_batch(self, *_args, **_kwargs):
        raise AssertionError("preserved chapter must not call translator")


class _Translator:
    src = "en"

    def translate_batch(self, sources, **_kwargs):
        return SimpleNamespace(
            translations=tuple(f"译：{source}" for source in sources),
            request_count=1,
        )


class _CapturingExecutor:
    def __init__(self):
        self.future = object()
        self.kwargs = None

    def submit(self, _callable, *_args, **kwargs):
        self.kwargs = kwargs
        return self.future


class _Glossary:
    def __init__(self, term):
        self.term = term

    def all_terms(self):
        return [self.term]


class _Polisher:
    def polish(self, *_args, **_kwargs):
        raise AssertionError("capturing executor must not run submitted work")


class TestSemanticRouting(unittest.TestCase):
    def test_preserved_chapter_commits_exact_source_without_model_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(
                directory,
                [
                    _classified_chapter(
                        0, "Ordinary-looking title", "Reference A.", action="preserve"
                    )
                ],
            )
            node = TranslateNode(
                translator=_NeverTranslator(),
                extractor=None,
                polisher=None,
                glossary=None,
                config=Config(),
                style_brief="",
                rolling_context=None,
            )
            shared = SimpleNamespace(segments_done=0, segments_total=1)
            outcome = node.execute(
                NodeRequest(
                    store=store,
                    node_id=NODE_TRANSLATE,
                    key="translate:0",
                    ci=0,
                    scope="chapter",
                    input_path="",
                    shared=shared,
                    total_chapters=1,
                )
            )

            self.assertEqual(store.load_chapter(0).text_segments[0].target, "Reference A.")
            self.assertEqual(store.load_progress(0).pending_polish, [])
            self.assertEqual(shared.segments_done, 1)
            self.assertTrue(outcome.fingerprint)

    def test_preserved_chapter_rejects_conflicting_pending_polish(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(
                directory,
                [_classified_chapter(0, "Reference", "Reference A.", action="preserve")],
            )
            progress = store.load_progress(0)
            progress.pending_polish = [{"start": 0, "count": 1}]
            store.save_progress(0, progress)
            node = TranslateNode(
                translator=_NeverTranslator(),
                extractor=None,
                polisher=None,
                glossary=None,
                config=Config(),
                style_brief="",
                rolling_context=None,
            )

            with self.assertRaisesRegex(ValueError, "仍有待润色译文"):
                node.execute(
                    NodeRequest(
                        store=store,
                        node_id=NODE_TRANSLATE,
                        key="translate:0",
                        ci=0,
                        scope="chapter",
                        input_path="",
                        shared=SimpleNamespace(segments_done=0, segments_total=1),
                        total_chapters=1,
                    )
                )

    def test_planner_schedules_only_passthrough_for_preserved_chapter(self):
        with tempfile.TemporaryDirectory() as directory:
            chapters = [
                _classified_chapter(0, "Reference", "Reference A.", action="preserve"),
                _classified_chapter(1, "Prose", "Narrative.", action="translate"),
            ]
            store = _store(directory, chapters)
            plan = Planner(build_workflow_definition()).build_plan(
                goal=GOAL_RUN_ALL,
                store=store,
                policy=WorkflowPolicy(polish=True),
                prescan=PrescanInputs(),
            )
            preserved = [entry for stage in plan.stages for entry in stage.entries if entry.ci == 0]
            translated = [
                entry.node_id for stage in plan.stages for entry in stage.entries if entry.ci == 1
            ]

            self.assertEqual(
                [(entry.node_id, entry.finalize_chapter) for entry in preserved],
                [(NODE_TRANSLATE, True)],
            )
            self.assertEqual(translated, [NODE_TRANSLATE, NODE_POLISH])

    def test_planner_rejects_source_changed_after_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(
                directory,
                [_classified_chapter(0, "Reference", "Reference A.", action="preserve")],
            )
            chapter = store.load_chapter(0)
            chapter.segments[0].source = "Changed."
            store.save_chapter(chapter)

            with self.assertRaisesRegex(IdentityMismatchError, "语义处理决策不一致"):
                Planner(build_workflow_definition()).build_plan(
                    goal=GOAL_RUN_ALL,
                    store=store,
                    policy=WorkflowPolicy(),
                    prescan=PrescanInputs(),
                )

    def test_style_sample_excludes_preserved_source(self):
        preserved = _classified_chapter(0, "Reference", "REFERENCE " * 100, action="preserve")
        translated = _classified_chapter(1, "Prose", "NARRATIVE " * 100, action="translate")
        document = Document(
            title="Book",
            fmt="text",
            source_lang="en",
            target_lang="zh",
            chapters=[preserved, translated],
        )

        sample = sample_text(document)

        self.assertIn("NARRATIVE", sample)
        self.assertNotIn("REFERENCE", sample)

    def test_polish_submission_carries_detached_request_term_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            chapter = _classified_chapter(0, "Prose", "Name appears.", action="translate")
            store = _store(directory, [chapter])
            term = GlossaryTerm(source="Name", target="姓名", locked=True)
            config = Config()
            config.pipeline.polish = True
            executor = _CapturingExecutor()
            shared = SimpleNamespace(
                segments_done=0,
                segments_total=1,
                polish_futures={},
            )
            node = TranslateNode(
                translator=_Translator(),
                extractor=None,
                polisher=_Polisher(),
                glossary=_Glossary(term),
                config=config,
                style_brief="",
                rolling_context=RollingContext(),
            )

            node.execute(
                NodeRequest(
                    store=store,
                    node_id=NODE_TRANSLATE,
                    key="translate:0",
                    ci=0,
                    scope="chapter",
                    input_path="",
                    executor=executor,
                    shared=shared,
                    total_chapters=1,
                )
            )

            future, snapshot = shared.polish_futures[(0, 0)]
            self.assertIs(future, executor.future)
            self.assertIs(executor.kwargs["glossary_terms"], snapshot)
            self.assertIsNot(snapshot[0], term)
            term.target = "已变更"
            self.assertEqual(snapshot[0].target, "姓名")

    def test_readiness_does_not_require_polish_node_for_preserved_chapter(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(
                directory,
                [_classified_chapter(0, "Reference", "Reference A.", action="preserve")],
            )
            chapter = store.load_chapter(0)
            chapter.segments[0].target = chapter.segments[0].source
            store.save_chapter(chapter)
            store.set_chapter_status(0, "done")
            state = store.load_state()
            for node_id in (
                NODE_PREPARE,
                NODE_ANALYZE,
                NODE_TITLES,
                NODE_DETERMINISTIC_QA,
                NODE_REPAIR,
                NODE_REPORT,
                "translate:0",
            ):
                state.nodes[node_id] = NodeState(node_id=node_id, status="succeeded")
            store.save_state(state)

            self.assertEqual(assemble_readiness_problems(store), [])

    def test_legacy_content_readiness_ignores_only_output_node_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            store = _store(
                directory,
                [_classified_chapter(0, "Reference", "Reference A.", action="preserve")],
            )
            chapter = store.load_chapter(0)
            chapter.segments[0].target = chapter.segments[0].source
            store.save_chapter(chapter)
            store.set_chapter_status(0, "done")
            state = store.load_state()
            for node_id in (
                NODE_PREPARE,
                NODE_ANALYZE,
                NODE_TITLES,
                NODE_DETERMINISTIC_QA,
                NODE_REPAIR,
                "translate:0",
            ):
                state.nodes[node_id] = NodeState(node_id=node_id, status="succeeded")
            for node_id in ("layout", NODE_REPORT, "assemble"):
                state.nodes[node_id] = NodeState(node_id=node_id, status="failed_retryable")
            store.save_state(state)

            self.assertTrue(assemble_readiness_problems(store))
            self.assertEqual(assemble_readiness_problems(store, require_output_nodes=False), [])
            state.nodes[NODE_PREPARE] = NodeState(node_id=NODE_PREPARE, status="failed_retryable")
            store.save_state(state)
            self.assertTrue(assemble_readiness_problems(store, require_output_nodes=False))

    def test_repair_ignores_preserved_chapter_even_if_input_contains_issue(self):
        class Translator:
            src = "en"

            def repair_issue(self, *_args, **_kwargs):
                raise AssertionError("preserved chapter must not call repair")

        with tempfile.TemporaryDirectory() as directory:
            store = _store(
                directory,
                [_classified_chapter(0, "Reference", "Reference A.", action="preserve")],
            )
            node = RepairNode(
                translator=Translator(),
                glossary=SimpleNamespace(all_terms=list),
                config=Config(),
            )
            outcome = node.execute(
                NodeRequest(
                    store=store,
                    node_id=NODE_REPAIR,
                    key=NODE_REPAIR,
                    ci=None,
                    scope="book",
                    input_path="",
                    artifacts={
                        "deterministic_qa": {
                            "issues": [
                                {
                                    "chapter": 0,
                                    "index": 0,
                                    "type": "untranslated",
                                    "detail": "source retained",
                                }
                            ]
                        }
                    },
                )
            )

            self.assertEqual(outcome.artifacts["issues"], [])


class TestSemanticTitleRouting(unittest.TestCase):
    def test_titles_leave_preserved_chapter_and_toc_label_original(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def complete_json(self, *_args, **_kwargs):
                self.calls += 1
                return {"titles": ["译名"]}

        with tempfile.TemporaryDirectory() as directory:
            chapters = [
                _classified_chapter(
                    0, "References", "Reference A.", action="preserve", toc_entry_id="p"
                ),
                _classified_chapter(
                    1, "Chapter", "Narrative.", action="translate", toc_entry_id="t"
                ),
            ]
            store = _store(
                directory,
                chapters,
                toc_entries=[
                    {
                        "entry_id": "p",
                        "title": "References",
                        "toc_path": "nav",
                        "node_index": 0,
                        "raw_href": "book.xhtml#references",
                        "boundary_position": 0,
                    },
                    {
                        "entry_id": "p-child",
                        "title": "Reference Details",
                        "toc_path": "nav",
                        "node_index": 1,
                        "parent_index": 0,
                        "raw_href": "book.xhtml#reference-details",
                        "boundary_position": 0,
                    },
                    {
                        "entry_id": "p-ncx",
                        "title": "NCX References",
                        "toc_path": "toc.ncx",
                        "node_index": 0,
                        "raw_href": "book.xhtml#references",
                        "boundary_position": 0,
                    },
                    {
                        "entry_id": "t",
                        "title": "Chapter",
                        "toc_path": "nav",
                        "node_index": 2,
                        "raw_href": "book.xhtml#chapter",
                        "boundary_position": 1,
                    },
                ],
            )
            for index in (0, 1):
                store.set_chapter_status(index, "done")
            client = Client()
            node = TitlesNode(
                client=client,
                config=Config(),
                src="en",
                tgt="zh",
                glossary=SimpleNamespace(all_terms=list),
            )

            node.execute(
                NodeRequest(
                    store=store,
                    node_id="titles",
                    key="titles",
                    ci=None,
                    scope="book",
                    input_path="",
                )
            )

            manifest = store.load_manifest()
            entries = {entry["entry_id"]: entry for entry in manifest["meta"]["toc_entries"]}
            self.assertIsNone(manifest["chapters"][0]["title_translated"])
            for entry_id in ("p", "p-child", "p-ncx"):
                self.assertNotIn("title_translated", entries[entry_id])
            self.assertEqual(manifest["chapters"][1]["title_translated"], "译名")
            self.assertEqual(entries["t"]["title_translated"], "译名")
            self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
