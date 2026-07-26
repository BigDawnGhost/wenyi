"""取证式 Review Agent Loop、全书证据索引和冲突仲裁测试。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from trans_novel.agents.review_loop import (
    ReviewAgentLoop,
    ReviewConflictArbiter,
    apply_review_arbitrations,
    build_conflict_groups,
    normalize_review_issues,
)
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.review_debug import DebugReviewRun
from trans_novel.pipeline.review_evidence import BookEvidenceIndex


def _config() -> Config:
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {
                    "strong": {"model": "strong"},
                    "cheap": {"model": "cheap"},
                },
            },
            "pipeline": {
                "review_agent_max_evidence_rounds": 2,
                "review_agent_tier": "strong",
            },
        }
    )


def _chapter(index: int, texts: list[tuple[str, str]]) -> Chapter:
    return Chapter(
        index=index,
        title=f"Chapter {index}",
        segments=[
            Segment(index=segment_index, source=source, target=target)
            for segment_index, (source, target) in enumerate(texts)
        ],
        meta={"source_digest": f"Digest {index}"},
    )


class TestBookEvidenceIndex(unittest.TestCase):
    def setUp(self):
        self.chapters = [
            _chapter(
                0,
                [
                    ("Ann arrived.", "安到了。"),
                    ("Anna left.", "安娜走了。"),
                    ("Ann spoke.", "安开口了。"),
                ],
            ),
            _chapter(1, [("ANN returned.", "安回来了。"), ("End.", "结束。")]),
        ]
        self.term = GlossaryTerm(source="Ann", target="安", aliases=["Annie"], type="人物")
        self.index = BookEvidenceIndex(
            self.chapters,
            [self.term],
            {"style_guide": "克制", "book_synopsis": "安离开后归来。"},
        )

    def test_selected_occurrences_use_book_order_alias_and_ascii_boundaries(self):
        result = self.index.term_occurrences(
            {
                "term": "Annie",
                "selectors": [1, 2, "last"],
                "context_radius": 0,
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["canonical_term"], "Ann")
        self.assertEqual(result["total_matches"], 3)
        self.assertEqual(
            [item["ordinal"] for item in result["selected"]],
            [1, 2, 3],
        )
        selected_sources = [item["source"] for item in result["selected"]]
        self.assertNotIn("Anna left.", selected_sources)

    def test_term_tool_does_not_return_unselected_occurrences(self):
        result = self.index.term_occurrences({"term": "Ann", "selectors": [1], "context_radius": 0})
        payload = json.dumps(result, ensure_ascii=False)

        self.assertIn("Ann arrived.", payload)
        self.assertNotIn("Ann spoke.", payload)
        self.assertNotIn("ANN returned.", payload)

    def test_glossary_tool_returns_only_requested_canonical_term(self):
        result = self.index.execute(
            {
                "request_id": "glossary-1",
                "tool": "glossary_term",
                "arguments": {"term": "Annie"},
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["request_id"], "glossary-1")
        self.assertEqual(result["term"]["source"], "Ann")
        self.assertEqual(result["term"]["target"], "安")
        self.assertEqual(result["term"]["aliases"], ["Annie"])
        self.assertEqual(
            BookEvidenceIndex.evidence_refs(result),
            {result["term"]["ref"]},
        )

    def test_exact_source_wins_over_another_terms_same_alias(self):
        other = GlossaryTerm(source="Anne", target="安妮", aliases=["Ann"], type="人物")
        index = BookEvidenceIndex(self.chapters, [self.term, other], {})

        term, ambiguous = index.canonical_term("Ann")

        self.assertIs(term, self.term)
        self.assertEqual(ambiguous, [])

    def test_exact_case_sensitive_source_wins_and_normalized_collision_is_ambiguous(self):
        upper = GlossaryTerm(source="ANN", target="甲", aliases=["Alice"], type="人物")
        title = GlossaryTerm(source="Ann", target="乙", aliases=["Annie"], type="人物")
        index = BookEvidenceIndex(
            [_chapter(0, [("Alice arrived.", "甲到了。"), ("Annie left.", "乙走了。")])],
            [upper, title],
            {},
        )

        self.assertIs(index.canonical_term("ANN")[0], upper)
        self.assertIs(index.canonical_term("Ann")[0], title)
        term, ambiguous = index.canonical_term("ann")
        self.assertIsNone(term)
        self.assertEqual(ambiguous, ["ANN", "Ann"])
        self.assertNotEqual(
            index.glossary_term({"term": "ANN"})["term"]["ref"],
            index.glossary_term({"term": "Ann"})["term"]["ref"],
        )
        self.assertEqual(
            index.term_occurrences({"term": "ANN", "selectors": [1]})["selected"][0]["source"],
            "Alice arrived.",
        )
        self.assertEqual(
            index.term_occurrences({"term": "Ann", "selectors": [1]})["selected"][0]["source"],
            "Annie left.",
        )

    def test_occurrence_result_includes_only_the_matched_glossary_entry(self):
        result = self.index.term_occurrences({"term": "Ann", "selectors": [1], "context_radius": 0})

        self.assertEqual(result["glossary_term"]["source"], "Ann")
        self.assertEqual(result["glossary_term"]["target"], "安")
        self.assertIn(result["glossary_term"]["ref"], BookEvidenceIndex.evidence_refs(result))

    def test_distinct_exact_sources_are_not_merged_into_one_conflict_key(self):
        upper = GlossaryTerm(source="ANN", target="甲", type="人物")
        title = GlossaryTerm(source="Ann", target="乙", type="人物")
        evidence = BookEvidenceIndex(self.chapters, [upper, title], {})
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"chunk-{index}",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": proposed,
                    "consistency": {
                        "kind": "term",
                        "subject_source": source,
                        "proposed_value": proposed,
                    },
                }
                for index, (source, proposed) in enumerate((("ANN", "甲"), ("Ann", "乙")))
            ],
            evidence,
        )

        self.assertNotEqual(
            issues[0]["consistency"]["key"],
            issues[1]["consistency"]["key"],
        )
        self.assertEqual(build_conflict_groups(issues), [])

        ambiguous_issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"ambiguous-{index}",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": proposed,
                    "consistency": {
                        "kind": "term",
                        "subject_source": "ann",
                        "proposed_value": proposed,
                    },
                }
                for index, proposed in enumerate(("甲", "乙"))
            ],
            evidence,
        )
        self.assertTrue(
            all(issue["consistency"]["auto_arbitration"] is False for issue in ambiguous_issues)
        )
        self.assertEqual(build_conflict_groups(ambiguous_issues), [])

    def test_book_context_has_stable_refs_and_rejects_unknown_chapter(self):
        style = self.index.execute(
            {
                "request_id": "style-1",
                "tool": "book_context",
                "arguments": {"section": "style_guide"},
            }
        )
        digest = self.index.execute(
            {
                "request_id": "digest-1",
                "tool": "book_context",
                "arguments": {"section": "chapter_digest", "chapter": 1},
            }
        )
        unknown = self.index.book_context({"section": "chapter_digest", "chapter": 99})

        self.assertEqual(BookEvidenceIndex.evidence_refs(style), {"book:style_guide"})
        self.assertEqual(
            BookEvidenceIndex.evidence_refs(digest),
            {"book:chapter_digest:ch1"},
        )
        self.assertEqual(unknown, {"ok": False, "error": "chapter_not_found"})

    def test_oversized_evidence_result_is_rejected(self):
        long = "x" * 5000
        index = BookEvidenceIndex(
            [_chapter(0, [(f"Ann {i} {long}", long) for i in range(8)])],
            [self.term],
            {},
        )
        result = index.execute(
            {
                "request_id": "large-1",
                "tool": "term_occurrences",
                "arguments": {
                    "term": "Ann",
                    "selectors": list(range(1, 9)),
                    "context_radius": 2,
                },
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "evidence_result_too_large")

    def test_segment_context_crosses_chapter_boundary(self):
        result = self.index.segment_context({"chapter": 1, "index": 0, "before": 1, "after": 1})

        self.assertTrue(result["ok"])
        self.assertEqual(
            [segment["source"] for segment in result["segments"]],
            ["Ann spoke.", "ANN returned.", "End."],
        )


class TestReadonlyGlossarySnapshot(unittest.TestCase):
    def test_reads_committed_wal_without_touching_formal_database_files(self):
        """只读 Review 快照必须包含尚未 checkpoint 的已提交 WAL。"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "glossary.db")
            writer = GlossaryStore(path)
            try:
                writer.upsert_term(
                    GlossaryTerm(source="Ann", target="安", type="人物"),
                    chapter=0,
                )
                watched = [path, f"{path}-wal", f"{path}-shm"]
                before = {
                    item: Path(item).read_bytes() if os.path.exists(item) else None
                    for item in watched
                }

                terms = GlossaryStore.load_terms_readonly(path)

                after = {
                    item: Path(item).read_bytes() if os.path.exists(item) else None
                    for item in watched
                }
                self.assertEqual([(term.source, term.target) for term in terms], [("Ann", "安")])
                self.assertEqual(after, before)
            finally:
                writer.close()

    def test_retries_when_checkpoint_changes_db_and_wal_between_copies(self):
        """DB/WAL 跨文件复制若撞上 checkpoint，不得接受混合时点快照。"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "glossary.db")
            writer = GlossaryStore(path)
            try:
                writer.upsert_term(
                    GlossaryTerm(source="Ann", target="安", type="人物"),
                    chapter=0,
                )
                writer.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.upsert_term(
                    GlossaryTerm(source="Bob", target="鲍勃", type="人物"),
                    chapter=0,
                )
                real_copy = shutil.copy2
                checkpointed = False

                def copy_with_checkpoint(source, target, *args, **kwargs):
                    nonlocal checkpointed
                    result = real_copy(source, target, *args, **kwargs)
                    if source == path and not checkpointed:
                        checkpointed = True
                        writer.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    return result

                with patch(
                    "trans_novel.glossary.store.shutil.copy2",
                    side_effect=copy_with_checkpoint,
                ):
                    terms = GlossaryStore.load_terms_readonly(path)

                self.assertTrue(checkpointed)
                self.assertEqual(
                    [(term.source, term.target) for term in terms],
                    [("Ann", "安"), ("Bob", "鲍勃")],
                )
            finally:
                writer.close()


class TestDebugReviewRun(unittest.TestCase):
    def test_equal_timestamps_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = datetime(2026, 7, 27, 12, 30, tzinfo=timezone.utc)
            first = DebugReviewRun(directory, now=moment)
            second = DebugReviewRun(directory, now=moment)

            self.assertNotEqual(first.run_dir, second.run_dir)
            self.assertTrue(os.path.isdir(first.run_dir))
            self.assertTrue(os.path.isdir(second.run_dir))
            self.assertNotIn(":", os.path.basename(first.run_dir))


class TestReviewAgentLoop(unittest.TestCase):
    def _evidence(self) -> BookEvidenceIndex:
        return BookEvidenceIndex(
            [
                _chapter(
                    0,
                    [
                        ("Ann arrived.", "安到了。"),
                        ("Ann spoke.", "安开口了。"),
                    ],
                )
            ],
            [GlossaryTerm(source="Ann", target="安", type="人物")],
            {},
        )

    def test_requests_selected_evidence_then_confirms_and_adds(self):
        calls = 0

        def handler(messages, tier, json_mode):
            nonlocal calls
            calls += 1
            if calls == 1:
                return json.dumps(
                    {
                        "action": "request_evidence",
                        "requests": [
                            {
                                "request_id": "term-1",
                                "tool": "term_occurrences",
                                "arguments": {
                                    "term": "Ann",
                                    "selectors": [1],
                                    "context_radius": 0,
                                },
                            }
                        ],
                        "complete": False,
                    }
                )
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {
                            "candidate_id": "ch0-base0-candidate0",
                            "verdict": "confirmed",
                            "detail": "译名不统一",
                            "suggestion": "统一译为安",
                            "reason": "",
                            "consistency": {
                                "subject_source": "Ann",
                                "kind": "term",
                                "proposed_value": "安",
                            },
                            "evidence_refs": ["ch0:text0:seg0"],
                        }
                    ],
                    "new_issues": [
                        {
                            "index": 1,
                            "type": "pronoun",
                            "detail": "代词错误",
                            "suggestion": "改为她",
                            "consistency": {
                                "subject_source": "Ann",
                                "kind": "pronoun",
                                "proposed_value": "她",
                            },
                            "evidence_refs": [],
                        }
                    ],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            debug = DebugReviewRun(directory)
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                debug,
            ).review_chunk(
                chapter=0,
                chunk_base=0,
                sources=["Ann arrived.", "Ann spoke."],
                targets=["安到了。", "安开口了。"],
                initial_issues=[
                    {
                        "index": 0,
                        "type": "terminology",
                        "detail": "疑似译名错误",
                        "suggestion": "核对译名",
                    }
                ],
            )
            with open(
                debug.path("agents/chunk-ch0-base0-n2.json"),
                encoding="utf-8",
            ) as file:
                trace = json.load(file)
            with open(debug.path("events.jsonl"), encoding="utf-8") as file:
                events = [json.loads(line) for line in file]

        self.assertEqual(calls, 2)
        self.assertEqual(len(outcome.issues), 2)
        self.assertEqual(outcome.issues[0]["suggestion"], "统一译为安")
        self.assertIn("ch0:text0:seg0", outcome.issues[0]["evidence_refs"])
        self.assertEqual(outcome.issues[1]["origin"], "agent")
        self.assertEqual(outcome.fallback_reason, "")
        self.assertEqual(trace["status"], "finished")
        self.assertIn("messages", trace["turns"][0])
        self.assertIn("raw_response", trace["turns"][0])
        self.assertIn("parsed", trace["turns"][0])
        self.assertIn("evidence_results", trace["turns"][0])
        self.assertTrue(any(event["event"] == "review_evidence_supplied" for event in events))

    def test_dismissed_summary_is_self_contained_and_links_to_initial_candidate(self):
        initial = {
            "index": 0,
            "type": "terminology",
            "detail": "疑似译名错误",
            "suggestion": "核对译名",
        }

        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {
                            "candidate_id": "ch0-base0-candidate0",
                            "verdict": "dismissed",
                            "detail": "",
                            "suggestion": "",
                            "reason": "术语表和首处译法均支持当前译文。",
                            "consistency": {},
                            "evidence_refs": [],
                        }
                    ],
                    "new_issues": [],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            debug = DebugReviewRun(directory)
            debug.record_initial_issues(
                chapter=0,
                chunk_base=0,
                issues=[initial],
            )
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                debug,
            ).review_chunk(
                chapter=0,
                chunk_base=0,
                sources=["Ann arrived."],
                targets=["安到了。"],
                initial_issues=[initial],
            )
            debug.record_dismissed(
                chapter=0,
                chunk_base=0,
                issues=outcome.dismissed,
            )
            initial_rows, dismissed_rows = debug.result_snapshots()

        self.assertEqual(outcome.issues, [])
        self.assertEqual(
            dismissed_rows[0]["candidate_id"],
            initial_rows[0]["candidate_id"],
        )
        for field in ("type", "detail", "suggestion", "reason"):
            self.assertTrue(dismissed_rows[0][field])

    def test_current_segment_ref_is_visible_in_prompt(self):
        def handler(messages, tier, json_mode):
            self.assertIn('"ref": "ch0:text0:seg0"', messages[-1]["content"])
            self.assertIn("ref=ch0:text0:seg0", messages[-1]["content"])
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {
                            "candidate_id": "ch0-base0-candidate0",
                            "verdict": "confirmed",
                            "detail": "确认",
                            "suggestion": "修正",
                            "reason": "",
                            "consistency": {},
                            "evidence_refs": [],
                        }
                    ],
                    "new_issues": [],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                DebugReviewRun(directory),
            ).review_chunk(
                chapter=0,
                chunk_base=0,
                sources=["Ann arrived."],
                targets=["安到了。"],
                initial_issues=[
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "候选",
                        "suggestion": "修正",
                    }
                ],
            )

        self.assertEqual(outcome.issues[0]["evidence_refs"], ["ch0:text0:seg0"])

    def test_out_of_chunk_new_issue_falls_back_to_initial_candidates(self):
        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {
                            "candidate_id": "ch0-base0-candidate0",
                            "verdict": "dismissed",
                            "reason": "误报",
                            "detail": "",
                            "suggestion": "",
                            "consistency": {},
                            "evidence_refs": [],
                        }
                    ],
                    "new_issues": [
                        {
                            "index": 2,
                            "type": "missing",
                            "detail": "越界",
                            "suggestion": "补译",
                            "consistency": {},
                            "evidence_refs": [],
                        }
                    ],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        initial = {
            "index": 0,
            "type": "missing",
            "detail": "初审候选",
            "suggestion": "补译",
        }
        with tempfile.TemporaryDirectory() as directory:
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                DebugReviewRun(directory),
            ).review_chunk(
                chapter=0,
                chunk_base=0,
                sources=["Ann arrived."],
                targets=["安到了。"],
                initial_issues=[initial],
            )

        self.assertTrue(outcome.fallback_reason)
        self.assertEqual(outcome.issues[0]["detail"], "初审候选")
        self.assertTrue(outcome.issues[0]["agent_fallback"])

    def test_nonempty_invalid_consistency_falls_back(self):
        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {
                            "candidate_id": "ch0-base0-candidate0",
                            "verdict": "confirmed",
                            "detail": "候选",
                            "suggestion": "修正",
                            "reason": "",
                            "consistency": {
                                "kind": "typo",
                                "subject_source": "Ann",
                                "proposed_value": "安",
                            },
                            "evidence_refs": [],
                        }
                    ],
                    "new_issues": [],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        initial = {
            "index": 0,
            "type": "missing",
            "detail": "初审候选",
            "suggestion": "补译",
        }
        with tempfile.TemporaryDirectory() as directory:
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                DebugReviewRun(directory),
            ).review_chunk(
                chapter=0,
                chunk_base=0,
                sources=["Ann arrived."],
                targets=["安到了。"],
                initial_issues=[initial],
            )

        self.assertIn("invalid_consistency", outcome.fallback_reason)
        self.assertEqual(outcome.issues[0]["detail"], "初审候选")

    def test_third_evidence_request_after_two_rounds_falls_back(self):
        calls = 0

        def handler(messages, tier, json_mode):
            nonlocal calls
            calls += 1
            return json.dumps(
                {
                    "action": "request_evidence",
                    "requests": [
                        {
                            "request_id": f"request-{calls}",
                            "tool": "segment_context",
                            "arguments": {
                                "chapter": 0,
                                "index": 0,
                                "before": calls - 1,
                                "after": 0,
                            },
                        }
                    ],
                    "complete": False,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                DebugReviewRun(directory),
            ).review_chunk(
                chapter=0,
                chunk_base=0,
                sources=["Ann arrived."],
                targets=["安到了。"],
                initial_issues=[
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "候选",
                        "suggestion": "补译",
                    }
                ],
            )

        self.assertEqual(calls, 3)
        self.assertIn("evidence_round_limit", outcome.fallback_reason)

    def test_unknown_evidence_ref_falls_back(self):
        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {
                            "candidate_id": "ch0-base0-candidate0",
                            "verdict": "confirmed",
                            "detail": "候选",
                            "suggestion": "补译",
                            "reason": "",
                            "consistency": {},
                            "evidence_refs": ["invented:ref"],
                        }
                    ],
                    "new_issues": [],
                    "complete": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                DebugReviewRun(directory),
            ).review_chunk(
                chapter=0,
                chunk_base=0,
                sources=["Ann arrived."],
                targets=["安到了。"],
                initial_issues=[
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "候选",
                        "suggestion": "补译",
                    }
                ],
            )

        self.assertIn("unknown_evidence_ref", outcome.fallback_reason)


class TestReviewConflictArbiter(unittest.TestCase):
    def test_conflicting_cross_chunk_claims_are_arbitrated(self):
        evidence = BookEvidenceIndex(
            [_chapter(0, [("Ann.", "安。"), ("Ann.", "安妮。")])],
            [GlossaryTerm(source="Ann", target="安", type="人物")],
            {},
        )
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": 0,
                    "_chunk_id": "chunk-a",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": "用安",
                    "consistency": {
                        "kind": "term",
                        "subject_source": "Ann",
                        "proposed_value": "安",
                    },
                },
                {
                    "chapter": 0,
                    "index": 1,
                    "_chunk_id": "chunk-b",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": "用安妮",
                    "consistency": {
                        "kind": "term",
                        "subject_source": "Ann",
                        "proposed_value": "安妮",
                    },
                },
            ],
            evidence,
        )
        conflicts = build_conflict_groups(issues)
        self.assertEqual(len(conflicts), 1)
        issue_ids = [issue["issue_id"] for issue in conflicts[0]["issues"]]

        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "conflict_id": "review-conflict-0001",
                    "status": "suggested",
                    "recommended_value": "安",
                    "reason": "沿用首次出现和术语表。",
                    "evidence_refs": [],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            result = ReviewConflictArbiter(
                FakeClient(handler=handler),
                _config(),
                evidence,
                DebugReviewRun(directory),
            ).arbitrate(conflicts[0])

        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["recommended_value"], "安")
        self.assertEqual(result["supported_issue_ids"], [issue_ids[0]])
        self.assertEqual(result["rejected_issue_ids"], [issue_ids[1]])

    def test_all_issues_with_the_winning_value_are_kept(self):
        """仲裁只选值，系统必须保留提出同一胜出值的全部问题。"""
        evidence = BookEvidenceIndex(
            [_chapter(0, [("Ann A.", "安。"), ("Ann B.", "安妮。"), ("Ann C.", "安。")])],
            [GlossaryTerm(source="Ann", target="安", type="人物")],
            {},
        )
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"chunk-{index}",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": f"统一为{proposed}",
                    "consistency": {
                        "kind": "term",
                        "subject_source": "Ann",
                        "proposed_value": proposed,
                    },
                }
                for index, proposed in enumerate(("安", "安妮", "安"))
            ],
            evidence,
        )
        conflict = build_conflict_groups(issues)[0]

        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "conflict_id": conflict["conflict_id"],
                    "status": "suggested",
                    "recommended_value": "安",
                    "reason": "沿用多数且与术语表一致的译名。",
                    "evidence_refs": [],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            result = ReviewConflictArbiter(
                FakeClient(handler=handler),
                _config(),
                evidence,
                DebugReviewRun(directory),
            ).arbitrate(conflict)

        self.assertEqual(
            result["supported_issue_ids"],
            [issues[0]["issue_id"], issues[2]["issue_id"]],
        )
        self.assertEqual(result["rejected_issue_ids"], [issues[1]["issue_id"]])

    def test_recommended_value_uses_the_exact_existing_proposal_spelling(self):
        evidence = BookEvidenceIndex(
            [_chapter(0, [("Agency A.", "NASA。"), ("Agency B.", "ESA。")])],
            [],
            {},
        )
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"chunk-{index}",
                    "type": "terminology",
                    "detail": "机构简称不统一",
                    "suggestion": proposed,
                    "consistency": {
                        "kind": "fixed",
                        "subject_source": "agency",
                        "proposed_value": proposed,
                    },
                }
                for index, proposed in enumerate(("NASA", "ESA"))
            ],
            evidence,
        )
        conflict = build_conflict_groups(issues)[0]

        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "conflict_id": conflict["conflict_id"],
                    "status": "suggested",
                    "recommended_value": "nasa",
                    "reason": "选择已有的 NASA 写法。",
                    "evidence_refs": [],
                    "complete": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            result = ReviewConflictArbiter(
                FakeClient(handler=handler),
                _config(),
                evidence,
                DebugReviewRun(directory),
            ).arbitrate(conflict)

        self.assertEqual(result["recommended_value"], "NASA")

    def test_arbiter_must_requery_inherited_evidence_before_citing_it(self):
        """块级 Agent 的 opaque ref 不等于仲裁器已经看过该证据。"""
        evidence = BookEvidenceIndex(
            [_chapter(0, [("Ann.", "安。"), ("Ann.", "安妮。")])],
            [GlossaryTerm(source="Ann", target="安", type="人物")],
            {},
        )
        glossary_result = evidence.glossary_term({"term": "Ann"})
        inherited_ref = next(iter(BookEvidenceIndex.evidence_refs(glossary_result)))
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"chunk-{index}",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": f"统一为{proposed}",
                    "evidence_refs": [inherited_ref],
                    "consistency": {
                        "kind": "term",
                        "subject_source": "Ann",
                        "proposed_value": proposed,
                    },
                }
                for index, proposed in enumerate(("安", "安妮"))
            ],
            evidence,
        )
        conflict = build_conflict_groups(issues)[0]

        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "conflict_id": conflict["conflict_id"],
                    "status": "suggested",
                    "recommended_value": "安",
                    "reason": "引用了未重新取得的术语证据。",
                    "evidence_refs": [inherited_ref],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            result = ReviewConflictArbiter(
                FakeClient(handler=handler),
                _config(),
                evidence,
                DebugReviewRun(directory),
            ).arbitrate(conflict)

        self.assertEqual(result["status"], "unresolved")
        self.assertIn("unknown_evidence_ref", result["reason"])

    def test_arbiter_prompt_samples_each_proposal_instead_of_embedding_all_issues(self):
        texts = [(f"SOURCE-{index:03d}", f"TARGET-{index:03d}") for index in range(12)]
        evidence = BookEvidenceIndex([_chapter(0, texts)], [], {})
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"chunk-{index}",
                    "type": "terminology",
                    "detail": "译名问题",
                    "suggestion": f"统一为{proposed}",
                    "consistency": {
                        "kind": "term",
                        "subject_source": "Ann",
                        "proposed_value": proposed,
                    },
                }
                for index, proposed in enumerate(["安"] * 10 + ["安妮"] * 2)
            ],
            evidence,
        )
        conflict = build_conflict_groups(issues)[0]

        def handler(messages, tier, json_mode):
            prompt = messages[-1]["content"]
            self.assertIn('"issue_count": 10', prompt)
            for sampled in ("SOURCE-000", "SOURCE-004", "SOURCE-009"):
                self.assertIn(sampled, prompt)
            for omitted in ("SOURCE-001", "SOURCE-002", "SOURCE-003", "SOURCE-005"):
                self.assertNotIn(omitted, prompt)
            return json.dumps(
                {
                    "action": "final",
                    "conflict_id": conflict["conflict_id"],
                    "status": "suggested",
                    "recommended_value": "安",
                    "reason": "抽样证据一致。",
                    "evidence_refs": [],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            result = ReviewConflictArbiter(
                FakeClient(handler=handler),
                _config(),
                evidence,
                DebugReviewRun(directory),
            ).arbitrate(conflict)

        self.assertEqual(result["status"], "suggested")
        self.assertEqual(len(result["supported_issue_ids"]), 10)

    def test_oversized_arbitration_sample_falls_back_without_model_call(self):
        long_text = "很长的证据" * 500
        texts = [(f"{index}-{long_text}", long_text) for index in range(32)]
        evidence = BookEvidenceIndex([_chapter(0, texts)], [], {})
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": index,
                    "_chunk_id": f"chunk-{index}",
                    "type": "terminology",
                    "detail": long_text,
                    "suggestion": long_text,
                    "consistency": {
                        "kind": "fixed",
                        "subject_source": "口号",
                        "proposed_value": f"版本-{index}",
                    },
                }
                for index in range(32)
            ],
            evidence,
        )
        conflict = build_conflict_groups(issues)[0]
        client = FakeClient(handler=lambda m, t, j: self.fail("不应调用模型"))

        with tempfile.TemporaryDirectory() as directory:
            result = ReviewConflictArbiter(
                client,
                _config(),
                evidence,
                DebugReviewRun(directory),
            ).arbitrate(conflict)

        self.assertEqual(client.calls, [])
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("大小上限", result["reason"])

    def test_arbitration_is_applied_to_the_final_issue_view(self):
        issues = [
            {"issue_id": "review-00001", "detail": "保留", "suggestion": "统一为安"},
            {"issue_id": "review-00002", "detail": "改写", "suggestion": "统一为安妮"},
        ]
        final, rejected = apply_review_arbitrations(
            issues,
            [
                {
                    "conflict_id": "review-conflict-0001",
                    "status": "suggested",
                    "recommended_value": "安",
                    "reason": "采用首次译名。",
                    "supported_issue_ids": ["review-00001"],
                    "rejected_issue_ids": ["review-00002"],
                }
            ],
        )

        self.assertEqual(
            [issue["issue_id"] for issue in final],
            ["review-00001", "review-00002"],
        )
        self.assertEqual([issue["issue_id"] for issue in rejected], ["review-00002"])
        self.assertEqual(final[0]["arbitration"]["recommended_value"], "安")
        self.assertEqual(final[1]["detail"], "该处相关表达需按终局仲裁统一为「安」。")
        self.assertEqual(final[1]["pre_arbitration_detail"], "改写")
        self.assertEqual(final[1]["suggestion"], "按终局仲裁将相关表达统一为「安」。")
        self.assertEqual(final[1]["pre_arbitration_suggestion"], "统一为安妮")

    def test_unresolved_arbitration_keeps_every_issue(self):
        issues = [
            {"issue_id": "review-00001", "detail": "甲"},
            {"issue_id": "review-00002", "detail": "乙"},
        ]
        final, rejected = apply_review_arbitrations(
            issues,
            [
                {
                    "conflict_id": "review-conflict-0001",
                    "status": "unresolved",
                    "recommended_value": "",
                    "reason": "证据不足。",
                    "issue_ids": ["review-00001", "review-00002"],
                    "supported_issue_ids": ["review-00001", "review-00002"],
                    "rejected_issue_ids": [],
                }
            ],
        )

        self.assertEqual(len(final), 2)
        self.assertEqual(rejected, [])
        self.assertTrue(all(issue["arbitration"]["status"] == "unresolved" for issue in final))

    def test_unproposed_suggested_value_falls_back_to_unresolved(self):
        evidence = BookEvidenceIndex(
            [_chapter(0, [("Ann.", "安。"), ("Ann.", "安妮。")])],
            [GlossaryTerm(source="Ann", target="安", type="人物")],
            {},
        )
        issues = normalize_review_issues(
            [
                {
                    "chapter": 0,
                    "index": 0,
                    "_chunk_id": "chunk-a",
                    "type": "terminology",
                    "detail": "甲",
                    "suggestion": "用安",
                    "consistency": {
                        "kind": "term",
                        "subject_source": "Ann",
                        "proposed_value": "安",
                    },
                },
                {
                    "chapter": 0,
                    "index": 1,
                    "_chunk_id": "chunk-b",
                    "type": "terminology",
                    "detail": "乙",
                    "suggestion": "用安妮",
                    "consistency": {
                        "kind": "term",
                        "subject_source": "Ann",
                        "proposed_value": "安妮",
                    },
                },
            ],
            evidence,
        )
        conflict = build_conflict_groups(issues)[0]

        def handler(messages, tier, json_mode):
            self.assertIn("glossary_term", messages[0]["content"])
            self.assertIn('"source": "Ann."', messages[-1]["content"])
            return json.dumps(
                {
                    "action": "final",
                    "conflict_id": conflict["conflict_id"],
                    "status": "suggested",
                    "recommended_value": "安娜",
                    "reason": "错误地提出第三种值。",
                    "evidence_refs": [],
                    "complete": True,
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            result = ReviewConflictArbiter(
                FakeClient(handler=handler),
                _config(),
                evidence,
                DebugReviewRun(directory),
            ).arbitrate(conflict)

        self.assertEqual(result["status"], "unresolved")
        self.assertIn("recommended_value_not_proposed", result["reason"])


if __name__ == "__main__":
    unittest.main()
