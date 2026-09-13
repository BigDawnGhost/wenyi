from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.assemble.report import build_report
from trans_novel.config import Config
from trans_novel.epub.slots import EpubSegmentState, EpubTextSlot
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.llm.errors import AllModelsFailedError, ProviderError
from trans_novel.llm.retrying import classify_retry
from trans_novel.pipeline.contracts import NodeRequest
from trans_novel.pipeline.nodes.finish import AssembleNode
from trans_novel.pipeline.nodes.repair import RepairNode
from trans_novel.pipeline.planning import assemble_input_fingerprint
from trans_novel.pipeline.quality import LintIssue
from trans_novel.pipeline.state import (
    NODE_DETERMINISTIC_QA,
    NODE_REPAIR,
    NodeState,
    RunIdentity,
    RunStore,
)


class _Glossary:
    def all_terms(self):
        return []

    def open_conflicts(self):
        return []

    def low_confidence_terms(self):
        return []

    def stats(self):
        return {"terms": 0}


class _RepairTranslator:
    src = "en"

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def repair_issue(self, source, current_target, **kwargs):
        self.calls.append((source, current_target, kwargs))
        response = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _PermanentProviderError(ProviderError):
    status_code = 401


class TestRepairContracts(unittest.TestCase):
    def _store(self, source, target, *, epub=False):
        directory = tempfile.TemporaryDirectory()
        store = RunStore(directory.name)
        segment = Segment(index=0, source=source, target=target)
        if epub:
            segment.epub_state = EpubSegmentState(
                resource_href="chapter.xhtml",
                resource_sha256="source",
                block_path=(0,),
                block_fingerprint="block",
                parse_mode="xml",
                slots=[
                    EpubTextSlot(id="slot", field="text", source_value=source, target_value=target)
                ],
                slot_contract_sha256="contract",
            )
        doc = Document(
            title="Repair Book",
            fmt="epub" if epub else "text",
            source_lang="en",
            target_lang="zh",
            source_path="book.txt",
            meta={"epub_schema": 4} if epub else {},
            chapters=[Chapter(index=0, title="Chapter", segments=[segment])],
        )
        raw = store.stage_document(
            doc,
            RunIdentity(source_bytes_sha256="source", source_lang="en", target_lang="zh"),
        )
        raw["initialized"] = True
        raw["nodes"] = {
            NODE_DETERMINISTIC_QA: NodeState(node_id=NODE_DETERMINISTIC_QA).model_dump(mode="json")
        }
        store.save_manifest(raw)
        progress = store.load_progress(0)
        progress.status = "done"
        store.save_progress(0, progress)
        self.addCleanup(directory.cleanup)
        return store

    @staticmethod
    def _request(store):
        return NodeRequest(
            store=store,
            node_id=NODE_REPAIR,
            key=NODE_REPAIR,
            ci=None,
            scope="book",
            input_path="book.txt",
        )

    def _run(self, store, translator):
        return RepairNode(translator=translator, glossary=_Glossary()).execute(self._request(store))

    @staticmethod
    def _record(store):
        return next(iter(store.load_progress(0).repair_ledger.values()))

    def test_one_issue_succeeds_on_third_repair_call(self):
        store = self._store('"Hello world."', "你好。")
        translator = _RepairTranslator(["仍无引号", "还是无引号", "“你好。”"])
        self._run(store, translator)
        self.assertEqual(len(translator.calls), 3)
        self.assertEqual(self._record(store).status, "resolved")

    def test_multiple_issues_use_independent_budgets_sequentially(self):
        store = self._store('"He has 24 apples."', "他有苹果。")
        translator = _RepairTranslator(["“他有苹果。”", "“他有24个苹果。”"])
        self._run(store, translator)
        self.assertEqual(len(translator.calls), 2)
        records = list(store.load_progress(0).repair_ledger.values())
        self.assertEqual({item.status for item in records}, {"resolved"})
        self.assertEqual({item.attempts for item in records}, {1})

    def test_book_issues_sort_by_segment_index_without_reordering_lint_emissions(self):
        store = self._store("first", "target")
        chapter = store.load_chapter(0)
        chapter.segments[0].index = 10
        chapter.segments.append(Segment(index=2, source="second", target="target"))
        store.save_chapter(chapter)
        emitted = [
            LintIssue(1, "quote_loss", "second quote"),
            LintIssue(0, "too_long", "first length"),
            LintIssue(1, "number_mismatch", "second number"),
        ]
        node = RepairNode(translator=_RepairTranslator(), glossary=_Glossary())
        with patch("trans_novel.pipeline.nodes.repair.lint_targets", return_value=emitted):
            issues = node._book_issues(store)[0]
        self.assertEqual(
            [(item.index, item.type) for item in issues],
            [(2, "quote_loss"), (2, "number_mismatch"), (10, "too_long")],
        )

    def test_candidate_introducing_new_issue_is_rejected_and_not_written(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        translator = _RepairTranslator(["He has 24 apples."])
        self._run(store, translator)
        self.assertEqual(store.load_chapter(0).segments[0].target, "他有苹果。")
        self.assertEqual(self._record(store).status, "accepted_after_exhaustion")

    def test_ten_unsuccessful_attempts_are_exhausted_and_output_continues(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        outcome = self._run(store, _RepairTranslator(["仍然没有数字"]))
        self.assertEqual(self._record(store).attempts, 10)
        self.assertEqual(self._record(store).status, "accepted_after_exhaustion")
        self.assertIn("repair", outcome.artifacts)

    def test_provider_failure_defers_repair_for_resume(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        translator = _RepairTranslator([AllModelsFailedError(()), "他有24个苹果。"])

        outcome = self._run(store, translator)

        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(self._record(store).attempts, 1)
        self.assertEqual(self._record(store).status, "pending")
        self.assertTrue(outcome.artifacts["repair"]["deferred"])
        self.assertEqual(outcome.artifacts["repair"]["accepted_after_exhaustion"], 0)
        self.assertEqual(store.load_chapter(0).segments[0].target, "他有苹果。")
        self.assertTrue(build_report(store, _Glossary())["repair"]["deferred"])

        outcome = self._run(store, translator)

        self.assertEqual(len(translator.calls), 2)
        self.assertFalse(outcome.artifacts["repair"]["deferred"])
        self.assertEqual(self._record(store).status, "resolved")
        self.assertEqual(store.load_chapter(0).segments[0].target, "他有24个苹果。")
        self.assertFalse(build_report(store, _Glossary())["repair"]["deferred"])

    def test_permanent_provider_failure_defers_repair_without_retries(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        provider_error = _PermanentProviderError("unauthorized")
        self.assertIsNone(classify_retry(provider_error))
        translator = _RepairTranslator([provider_error])
        outcome = self._run(store, translator)
        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(self._record(store).attempts, 1)
        self.assertEqual(self._record(store).status, "pending")
        self.assertEqual(outcome.artifacts["repair"]["accepted_after_exhaustion"], 0)
        self.assertEqual(store.load_chapter(0).segments[0].target, "他有苹果。")

    def test_repair_programming_error_propagates(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        with self.assertRaisesRegex(RuntimeError, "bug"):
            self._run(store, _RepairTranslator([RuntimeError("bug")]))

    def test_initial_translation_failure_uses_exact_source_fallback_before_repair(self):
        store = self._store("He has 24 apples.", "He has 24 apples.")
        translator = _RepairTranslator(["他有24个苹果。"])
        self._run(store, translator)
        self.assertEqual(translator.calls[0][1], "He has 24 apples.")
        self.assertEqual(store.load_chapter(0).segments[0].target, "他有24个苹果。")

    def test_interruption_resumes_at_next_persisted_attempt(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        translator = _RepairTranslator(["他有24个苹果。"])
        node = RepairNode(translator=translator, glossary=_Glossary())
        node._register(node._book_issues(store), store)
        progress = store.load_progress(0)
        for record in progress.repair_ledger.values():
            record.attempts = 4
        store.save_progress(0, progress)
        node.execute(self._request(store))
        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(self._record(store).attempts, 5)

    def test_completed_chapter_is_relinted_without_ordinary_translation(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        translator = _RepairTranslator(["他有24个苹果。"])
        self._run(store, translator)
        self.assertEqual(len(translator.calls), 1)
        self.assertEqual(store.load_chapter(0).segments[0].target, "他有24个苹果。")

    def test_target_reads_committed_plain_text_target(self):
        segment = Segment(index=0, source="Hello", target="已提交")
        self.assertEqual(RepairNode._target(segment), "已提交")

    def test_target_reads_committed_epub_slot_targets(self):
        segment = Segment(index=0, source="Hello world", target="stale")
        segment.epub_state = EpubSegmentState(
            resource_href="chapter.xhtml",
            resource_sha256="source",
            block_path=(0,),
            block_fingerprint="block",
            parse_mode="xml",
            slots=[
                EpubTextSlot(
                    id="first",
                    field="text",
                    source_value="Hello ",
                    target_value="你好 ",
                ),
                EpubTextSlot(
                    id="second",
                    field="tail",
                    source_value="world",
                    target_value="世界",
                ),
            ],
            slot_contract_sha256="contract",
        )
        self.assertEqual(RepairNode._target(segment), "你好 世界")

    def test_repaired_epub_target_and_slot_remain_consistent(self):
        store = self._store("He has 24", "他有", epub=True)
        self._run(store, _RepairTranslator(["他有24"]))
        segment = store.load_chapter(0).segments[0]
        self.assertEqual(segment.target, "他有24")
        self.assertEqual(segment.epub_state.slots[0].target_value, "他有24")

    def test_report_requires_no_user_action_and_distinguishes_exhaustion(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        self._run(store, _RepairTranslator(["仍然没有数字"]))
        report = build_report(store, _Glossary())
        self.assertFalse(report["requires_user_action"])
        self.assertEqual(report["repair"]["accepted_after_exhaustion"], 1)
        self.assertEqual(report["deterministic_issues"], [])

    def test_mono_and_bilingual_outputs_are_requested_after_exhaustion(self):
        store = self._store("He has 24 apples.", "他有苹果。")
        config = Config.from_dict({"llm": fake_llm_dict()})
        config.output.mono = True
        config.output.bilingual.enabled = True
        node = AssembleNode(output=config.output, out_format="txt", output_digest="theme-digest")
        with patch(
            "trans_novel.pipeline.nodes.finish.assemble_outputs",
            return_value=["mono.txt", "bilingual.txt"],
        ) as assemble_outputs:
            outcome = node.execute(self._request(store))
        self.assertEqual(outcome.artifacts["outputs"], ["mono.txt", "bilingual.txt"])
        self.assertEqual(
            assemble_outputs.call_args.args[2],
            [(None, False), (None, True)],
        )
        self.assertEqual(
            outcome.fingerprint,
            assemble_input_fingerprint(
                "他有苹果。",
                mono=True,
                bilingual=True,
                out_format="txt",
                bilingual_order="target_first",
                output_digest="theme-digest",
            ),
        )


if __name__ == "__main__":
    unittest.main()
