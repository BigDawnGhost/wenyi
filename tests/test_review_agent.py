"""Evidence-loop, whole-book evidence-index and arbitration tests."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from trans_novel.agents.review_fixer import (
    ProvisionalPatch,
    ReviewFixer,
    ReviewFixerProtocolError,
)
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
from trans_novel.llm.routing import inference_snapshot
from trans_novel.review.evidence import BookEvidenceIndex
from trans_novel.review.run_store import ReviewRunStore, review_candidate_id


def _config() -> Config:
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {
                    "default_strong": {"provider": "default", "model": "strong"},
                    "default_cheap": {"provider": "default", "model": "cheap"},
                },
            },
            "pipeline": {
                "review_agent_max_evidence_rounds": 2,
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
        self.term = GlossaryTerm(source="Ann", target="安", aliases=["Annie"], type="person")
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
        other = GlossaryTerm(source="Anne", target="安妮", aliases=["Ann"], type="person")
        index = BookEvidenceIndex(self.chapters, [self.term, other], {})

        term, ambiguous = index.canonical_term("Ann")

        self.assertIs(term, self.term)
        self.assertEqual(ambiguous, [])

    def test_exact_case_sensitive_source_wins_and_normalized_collision_is_ambiguous(self):
        upper = GlossaryTerm(source="ANN", target="甲", aliases=["Alice"], type="person")
        title = GlossaryTerm(source="Ann", target="乙", aliases=["Annie"], type="person")
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
        upper = GlossaryTerm(source="ANN", target="甲", type="person")
        title = GlossaryTerm(source="Ann", target="乙", type="person")
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

    def test_target_overrides_are_visible_without_mutating_chapters(self):
        original = self.chapters[0].text_segments[0].target
        index = BookEvidenceIndex(
            self.chapters,
            [self.term],
            {},
            target_overrides={(0, 0): "影子修订。"},
        )

        context = index.segment_context({"chapter": 0, "index": 0, "before": 0, "after": 0})

        self.assertEqual(index.segments[0].target, "影子修订。")
        self.assertEqual(context["segments"][0]["target"], "影子修订。")
        self.assertEqual(context["segments"][0]["target_origin"], "shadow_override")
        self.assertEqual(context["segments"][0]["baseline_target"], original)
        self.assertEqual(self.chapters[0].text_segments[0].target, original)

    def test_formal_targets_are_labeled_without_duplicate_baseline_payload(self):
        context = self.index.segment_context({"chapter": 0, "index": 0, "before": 0, "after": 0})

        segment = context["segments"][0]
        self.assertEqual(segment["target_origin"], "formal")
        self.assertNotIn("baseline_target", segment)


class TestReviewFixer(unittest.TestCase):
    def _propose(self, payload: dict) -> ProvisionalPatch:
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                payload,
                ensure_ascii=False,
            )
        )
        return ReviewFixer(client, _config()).propose(
            1,
            "ch0:text1:seg1",
            0,
            1,
            "Original sentence.",
            "当前译文。",
            [
                {
                    "issue_id": "r1-review-00001",
                    "chapter": 0,
                    "index": 1,
                    "type": "mistranslation",
                    "detail": "原意不完整",
                    "suggestion": "补全信息",
                }
            ],
        )

    def _valid_payload(self, replacement: str = "修订后的完整译文。") -> dict:
        return {
            "segment_ref": "ch0:text1:seg1",
            "before_hash": ReviewFixer.target_hash("当前译文。"),
            "issue_ids": ["r1-review-00001"],
            "replacement": replacement,
            "complete": True,
        }

    def test_valid_full_segment_patch_is_provisional(self):
        patch = self._propose(self._valid_payload())

        self.assertEqual(patch.before, "当前译文。")
        self.assertEqual(patch.after, "修订后的完整译文。")
        self.assertEqual(patch.issue_ids, ("r1-review-00001",))
        self.assertEqual(patch.status, "provisional")

    def test_rejects_protocol_drift_and_unchanged_replacement(self):
        wrong_ids = self._valid_payload()
        wrong_ids["issue_ids"] = ["another-issue"]
        extra_field = self._valid_payload()
        extra_field["explanation"] = "不允许"

        for payload, reason in (
            (wrong_ids, "issue_ids_mismatch"),
            (extra_field, "unexpected_fields"),
            (self._valid_payload("当前译文。"), "unchanged_replacement"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ReviewFixerProtocolError, reason):
                    self._propose(payload)

    def test_rejects_dropped_dialogue_quotes(self):
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                {
                    "segment_ref": "ch0:text1:seg1",
                    "before_hash": ReviewFixer.target_hash("“当前译文。”"),
                    "issue_ids": ["r1-review-00001"],
                    "replacement": "修订后的完整译文。",
                    "complete": True,
                },
                ensure_ascii=False,
            )
        )

        with self.assertRaisesRegex(
            ReviewFixerProtocolError,
            "dropped_dialogue_quotes",
        ):
            ReviewFixer(client, _config()).propose(
                1,
                "ch0:text1:seg1",
                0,
                1,
                '"Original sentence."',
                "“当前译文。”",
                [
                    {
                        "issue_id": "r1-review-00001",
                        "chapter": 0,
                        "index": 1,
                        "type": "mistranslation",
                        "detail": "原意不完整",
                        "suggestion": "补全信息",
                    }
                ],
            )

    def test_allows_removing_target_quotes_absent_from_source(self):
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                {
                    "segment_ref": "ch0:text1:seg1",
                    "before_hash": ReviewFixer.target_hash("“当前译文。”"),
                    "issue_ids": ["r1-review-00001"],
                    "replacement": "修订后的完整译文。",
                    "complete": True,
                },
                ensure_ascii=False,
            )
        )

        patch = ReviewFixer(client, _config()).propose(
            1,
            "ch0:text1:seg1",
            0,
            1,
            "Original sentence.",
            "“当前译文。”",
            [
                {
                    "issue_id": "r1-review-00001",
                    "chapter": 0,
                    "index": 1,
                    "type": "added",
                    "detail": "原文没有对话引号",
                    "suggestion": "删除多余引号",
                }
            ],
        )

        self.assertEqual(patch.after, "修订后的完整译文。")


class TestReadonlyGlossarySnapshot(unittest.TestCase):
    def test_reads_committed_wal_without_touching_formal_database_files(self):
        """Read-only glossary snapshots must include committed, uncheckpointed WAL data."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "glossary.db")
            writer = GlossaryStore(path)
            try:
                writer.upsert_term(
                    GlossaryTerm(source="Ann", target="安", type="person"),
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
        """A checkpoint during DB/WAL copying must not produce an accepted mixed-time snapshot."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "glossary.db")
            writer = GlossaryStore(path)
            try:
                writer.upsert_term(
                    GlossaryTerm(source="Ann", target="安", type="person"),
                    chapter=0,
                )
                writer.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.upsert_term(
                    GlossaryTerm(source="Bob", target="鲍勃", type="person"),
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


class TestReviewRunStore(unittest.TestCase):
    def test_candidate_id_format_with_and_without_round(self):
        self.assertEqual(
            review_candidate_id(2, 10, 3),
            "ch2-base10-candidate3",
        )
        self.assertEqual(
            review_candidate_id(2, 10, 3, 4),
            "r4-ch2-base10-candidate3",
        )

    def test_equal_timestamps_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = datetime(2026, 7, 27, 12, 30, tzinfo=timezone.utc)
            first = ReviewRunStore(directory, now=moment)
            second = ReviewRunStore(directory, now=moment)

            self.assertNotEqual(first.run_dir, second.run_dir)
            self.assertTrue(os.path.isdir(first.run_dir))
            self.assertTrue(os.path.isdir(second.run_dir))
            self.assertNotIn(":", os.path.basename(first.run_dir))

    def test_round_scopes_isolate_files_events_and_issue_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            for review_round, detail in ((1, "第一轮"), (2, "第二轮")):
                with debug.round_scope(review_round):
                    debug.write_json("agents/same.json", {"detail": detail})
                    debug.record_initial_issues(
                        chapter=0,
                        chunk_base=0,
                        issues=[
                            {
                                "index": 0,
                                "type": "missing",
                                "detail": detail,
                                "suggestion": "修复",
                            }
                        ],
                    )
                    debug.log_event("round_probe")

            first = json.loads(
                Path(debug.run_dir, "rounds/001/agents/same.json").read_text(encoding="utf-8")
            )
            second = json.loads(
                Path(debug.run_dir, "rounds/002/agents/same.json").read_text(encoding="utf-8")
            )
            first_issues, _ = debug.result_snapshots(1)
            second_issues, _ = debug.result_snapshots(2)
            events = [
                json.loads(line)
                for line in Path(debug.run_dir, "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(first["detail"], "第一轮")
        self.assertEqual(second["detail"], "第二轮")
        self.assertEqual(first_issues[0]["review_round"], 1)
        self.assertEqual(second_issues[0]["review_round"], 2)
        self.assertNotEqual(
            first_issues[0]["candidate_id"],
            second_issues[0]["candidate_id"],
        )
        self.assertEqual(
            [event["review_round"] for event in events],
            [1, 2],
        )

    @staticmethod
    def _usage_summary(calls: int, tokens: int) -> dict:
        """Build usage data with the same shape as usage_delta output."""
        return {
            "schema_version": 2,
            "by_provider": {},
            "by_model": {},
            "totals": {
                "calls": calls,
                "prompt_tokens": tokens,
                "completion_tokens": 0,
                "total_tokens": tokens,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": tokens,
            },
            "by_tier": {
                "cheap": {
                    "calls": calls,
                    "prompt_tokens": tokens,
                    "completion_tokens": 0,
                    "total_tokens": tokens,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": tokens,
                }
            },
            "by_stage": {
                "review.scan": {
                    "calls": calls,
                    "prompt_tokens": tokens,
                    "completion_tokens": 0,
                    "total_tokens": tokens,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": tokens,
                }
            },
        }

    def test_save_usage_merges_increments_across_resumes(self):
        """save_usage must merge persisted usage without loss across process resumes."""
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            debug.save_usage(self._usage_summary(2, 100))
            debug.save_usage(self._usage_summary(3, 50))
            with open(os.path.join(debug.run_dir, "usage.json"), encoding="utf-8") as file:
                saved = json.load(file)

        self.assertEqual(saved["totals"]["calls"], 5)
        self.assertEqual(saved["totals"]["total_tokens"], 150)
        self.assertEqual(saved["by_stage"]["review.scan"]["calls"], 5)

    def test_rebuild_snapshots_skips_stale_subchunks_contained_in_parent(self):
        """When parent and stale child chunks coexist, count once by rebuilding larger blocks
        first.
        """
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            debug.mark_chunk_done(
                "r1-ch0-base0-n55",
                {
                    "status": "finished",
                    "issues": [],
                    "initial_issues": [
                        {"index": 0, "type": "missing", "detail": "父块问题", "suggestion": "补译"}
                    ],
                    "dismissed": [
                        {"index": 1, "type": "terminology", "detail": "父块驳回", "suggestion": ""}
                    ],
                },
            )
            debug.mark_chunk_done(
                "r1-ch0-base0-n27",
                {
                    "status": "finished",
                    "issues": [],
                    "initial_issues": [
                        {"index": 0, "type": "missing", "detail": "陈旧子块", "suggestion": "补译"}
                    ],
                    "dismissed": [],
                },
            )
            debug.mark_chunk_done(
                "r1-ch0-base55-n5",
                {
                    "status": "finished",
                    "issues": [],
                    "initial_issues": [],
                    "dismissed": [
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "独立子块驳回",
                            "suggestion": "",
                        }
                    ],
                },
            )
            with debug.round_scope(1):
                debug.rebuild_snapshots_from_chunks(1)
            initial, dismissed = debug.result_snapshots(1)

        self.assertEqual([issue["detail"] for issue in initial], ["父块问题"])
        self.assertEqual(
            [issue["detail"] for issue in dismissed],
            ["父块驳回", "独立子块驳回"],
        )

    def test_rebuild_snapshots_is_idempotent_across_resumes(self):
        """Repeated process-style restores must not duplicate aggregated snapshots."""
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            debug.mark_chunk_done(
                "r1-ch0-base0-n55",
                {
                    "status": "finished",
                    "issues": [],
                    "initial_issues": [
                        {"index": 0, "type": "missing", "detail": "问题A", "suggestion": "补译"}
                    ],
                    "dismissed": [
                        {"index": 1, "type": "terminology", "detail": "驳回B", "suggestion": ""}
                    ],
                },
            )
            debug.mark_chunk_done(
                "r1-ch0-base55-n5",
                {
                    "status": "finished",
                    "issues": [],
                    "initial_issues": [
                        {"index": 0, "type": "missing", "detail": "问题C", "suggestion": "补译"}
                    ],
                    "dismissed": [],
                },
            )

            def snapshot_counts() -> tuple[int, int]:
                # Simulate a new process by rebuilding a fresh ReviewRunStore from disk.
                fresh = ReviewRunStore(directory)
                with fresh.round_scope(1):
                    fresh.rebuild_snapshots_from_chunks(1)
                initial, dismissed = fresh.result_snapshots(1)
                keys = [
                    (issue["review_round"], issue["chapter"], issue["index"], issue["candidate_id"])
                    for issue in [*initial, *dismissed]
                ]
                return len(keys), len(set(keys))

            first, first_unique = snapshot_counts()
            # Second and third restores must preserve row counts and uniqueness.
            for _ in range(2):
                count, unique = snapshot_counts()
                self.assertEqual(count, first)
                self.assertEqual(unique, first_unique)
            self.assertEqual(
                first_unique, first
            )  # No restore may introduce duplicate rows internally.

    def test_from_existing_restores_started_at_from_result(self):
        """Restore started_at from result.json on resume."""
        with tempfile.TemporaryDirectory() as directory:
            moment = datetime(2026, 7, 27, 12, 30, tzinfo=timezone.utc)
            debug = ReviewRunStore(directory, now=moment)
            debug.start(reviewed_content_digest="abc", metadata={})
            restored = ReviewRunStore._from_existing(debug.run_dir, debug.review_id)

        self.assertEqual(restored.started_at, debug.started_at)

    def test_load_json_reads_round_scoped_and_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            self.assertIsNone(debug.load_json("agents/r1-chunk-ch0-base0-n2.json"))
            with debug.round_scope(1):
                debug.write_json("agents/r1-chunk-ch0-base0-n2.json", {"status": "running"})
                loaded = debug.load_json("agents/r1-chunk-ch0-base0-n2.json")
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded["status"], "running")
            self.assertIsNone(debug.load_json("agents/r1-chunk-ch0-base0-n2.json"))


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
            [GlossaryTerm(source="Ann", target="安", type="person")],
            {},
        )

    def test_run_resumes_finished_trace_without_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            with debug.round_scope(1):
                debug.write_json(
                    "agents/r1-chunk-ch0-base0-n2.json",
                    {
                        "agent_id": "r1-chunk-ch0-base0-n2",
                        "stage": "review.verify",
                        "inference": inference_snapshot(_config().llm, ("review.verify",)),
                        "status": "finished",
                        "turns": [],
                        "result": {"issues": [], "dismissed": []},
                    },
                )
                calls = []
                loop = ReviewAgentLoop(
                    FakeClient(handler=lambda m, t, j: calls.append(1) or ""),
                    _config(),
                    self._evidence(),
                    debug,
                )
                outcome = loop.review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived.", "Ann spoke."],
                    targets=["安到了。", "安开口了。"],
                    initial_issues=[],
                    review_round=1,
                )
            self.assertEqual(calls, [])
            self.assertEqual(outcome.issues, [])

    def test_run_resumes_fallback_trace_without_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            with debug.round_scope(1):
                debug.write_json(
                    "agents/r1-chunk-ch0-base0-n2.json",
                    {
                        "agent_id": "r1-chunk-ch0-base0-n2",
                        "stage": "review.verify",
                        "inference": inference_snapshot(_config().llm, ("review.verify",)),
                        "status": "fallback",
                        "fallback_reason": "malformed_json: broken",
                        "turns": [],
                    },
                )
                calls = []
                loop = ReviewAgentLoop(
                    FakeClient(handler=lambda m, t, j: calls.append(1) or ""),
                    _config(),
                    self._evidence(),
                    debug,
                )
                outcome = loop.review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived.", "Ann spoke."],
                    targets=["安到了。", "安开口了。"],
                    initial_issues=[
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "术语不一致",
                            "suggestion": "统一",
                        }
                    ],
                    review_round=1,
                )
            self.assertEqual(calls, [])
            self.assertEqual(outcome.fallback_reason, "malformed_json: broken")
            self.assertEqual(len(outcome.issues), 1)
            self.assertTrue(outcome.issues[0]["agent_fallback"])
            self.assertEqual(outcome.issues[0]["fallback_reason"], "malformed_json: broken")
            self.assertEqual(outcome.issues[0]["origin"], "initial")

    def test_run_reissues_only_inflight_turn_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            with debug.round_scope(1):
                evidence_turn = {
                    "turn": 1,
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u"},
                    ],
                    "status": "responded",
                    "raw_response": json.dumps(
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
                        },
                        ensure_ascii=False,
                    ),
                    "parsed": {
                        "action": "request_evidence",
                        "requests": [
                            {
                                "request_id": "term-1",
                                "tool": "term_occurrences",
                                "arguments": {"term": "Ann", "selectors": [1], "context_radius": 0},
                            }
                        ],
                        "complete": False,
                    },
                    "json_repaired": False,
                    "evidence_results": [
                        {
                            "request_id": "term-1",
                            "tool": "term_occurrences",
                            "ok": True,
                            "occurrences": [],
                        }
                    ],
                }
                debug.write_json(
                    "agents/r1-chunk-ch0-base0-n2.json",
                    {
                        "agent_id": "r1-chunk-ch0-base0-n2",
                        "stage": "review.verify",
                        "inference": inference_snapshot(_config().llm, ("review.verify",)),
                        "status": "running",
                        "turns": [evidence_turn],
                    },
                )
                calls = []

                def handler(messages, tier, json_mode):
                    calls.append(messages)
                    assert (
                        messages[-1]["role"] == "user"
                        and "[Evidence tool results (JSON)]" in messages[-1]["content"]
                    )
                    return json.dumps(
                        {
                            "action": "final",
                            "decisions": [
                                {
                                    "candidate_id": "r1-ch0-base0-candidate0",
                                    "index": 0,
                                    "verdict": "confirmed",
                                    "reason": "ok",
                                }
                            ],
                            "complete": True,
                        },
                        ensure_ascii=False,
                    )

                loop = ReviewAgentLoop(
                    FakeClient(handler=handler), _config(), self._evidence(), debug
                )
                outcome = loop.review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived.", "Ann spoke."],
                    targets=["安到了。", "安开口了。"],
                    initial_issues=[
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "术语不一致",
                            "suggestion": "统一",
                        }
                    ],
                    review_round=1,
                )
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                outcome.issues,
                [
                    {
                        "index": 0,
                        "type": "terminology",
                        "detail": "术语不一致",
                        "suggestion": "统一",
                        "origin": "initial",
                        "candidate_id": "r1-ch0-base0-candidate0",
                        "consistency": {},
                        "evidence_refs": ["ch0:text0:seg0"],
                    }
                ],
            )
            with debug.round_scope(1):
                saved = debug.load_json("agents/r1-chunk-ch0-base0-n2.json")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["status"], "finished")
            self.assertEqual(len(saved["turns"]), 2)
            events = [
                json.loads(line)
                for line in Path(debug.run_dir, "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertIn(
                "review_agent_resumed",
                [event["event"] for event in events],
            )
            # Cached evidence rounds must not duplicate events already emitted by the earlier process.
            self.assertEqual(
                sum(1 for e in events if e["event"] == "review_evidence_supplied"),
                0,
            )

    def test_run_resumes_parsed_final_turn_without_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            with debug.round_scope(1):
                final_raw = json.dumps(
                    {
                        "action": "final",
                        "decisions": [
                            {
                                "candidate_id": "r1-ch0-base0-candidate0",
                                "index": 0,
                                "verdict": "confirmed",
                                "reason": "ok",
                            }
                        ],
                        "complete": True,
                    },
                    ensure_ascii=False,
                )
                debug.write_json(
                    "agents/r1-chunk-ch0-base0-n2.json",
                    {
                        "agent_id": "r1-chunk-ch0-base0-n2",
                        "stage": "review.verify",
                        "inference": inference_snapshot(_config().llm, ("review.verify",)),
                        "status": "running",
                        "turns": [
                            {
                                "turn": 1,
                                "messages": [
                                    {"role": "system", "content": "s"},
                                    {"role": "user", "content": "u"},
                                ],
                                "status": "responded",
                                "raw_response": final_raw,
                                "parsed": json.loads(final_raw),
                                "json_repaired": False,
                            }
                        ],
                    },
                )
                calls = []
                loop = ReviewAgentLoop(
                    FakeClient(handler=lambda m, t, j: calls.append(1) or ""),
                    _config(),
                    self._evidence(),
                    debug,
                )
                outcome = loop.review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived.", "Ann spoke."],
                    targets=["安到了。", "安开口了。"],
                    initial_issues=[
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "术语不一致",
                            "suggestion": "统一",
                        }
                    ],
                    review_round=1,
                )
            self.assertEqual(calls, [])
            self.assertEqual(outcome.issues[0]["candidate_id"], "r1-ch0-base0-candidate0")
            self.assertEqual(outcome.issues[0]["origin"], "initial")
            with debug.round_scope(1):
                saved = debug.load_json("agents/r1-chunk-ch0-base0-n2.json")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["status"], "finished")

    def test_run_resumes_reexecutes_evidence_without_llm_call(self):
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            with debug.round_scope(1):
                evidence_raw = json.dumps(
                    {
                        "action": "request_evidence",
                        "requests": [
                            {
                                "request_id": "term-1",
                                "tool": "term_occurrences",
                                "arguments": {"term": "Ann", "selectors": [1], "context_radius": 0},
                            }
                        ],
                        "complete": False,
                    },
                    ensure_ascii=False,
                )
                debug.write_json(
                    "agents/r1-chunk-ch0-base0-n2.json",
                    {
                        "agent_id": "r1-chunk-ch0-base0-n2",
                        "stage": "review.verify",
                        "inference": inference_snapshot(_config().llm, ("review.verify",)),
                        "status": "running",
                        "turns": [
                            {
                                "turn": 1,
                                "messages": [
                                    {"role": "system", "content": "s"},
                                    {"role": "user", "content": "u"},
                                ],
                                "status": "responded",
                                "raw_response": evidence_raw,
                                "parsed": json.loads(evidence_raw),
                                "json_repaired": False,
                            }
                        ],
                    },
                )
                calls = []

                def handler(m, t, j):
                    calls.append(1)
                    return json.dumps(
                        {
                            "action": "final",
                            "decisions": [
                                {
                                    "candidate_id": "r1-ch0-base0-candidate0",
                                    "index": 0,
                                    "verdict": "confirmed",
                                    "reason": "ok",
                                }
                            ],
                            "complete": True,
                        },
                        ensure_ascii=False,
                    )

                loop = ReviewAgentLoop(
                    FakeClient(handler=handler), _config(), self._evidence(), debug
                )
                outcome = loop.review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived.", "Ann spoke."],
                    targets=["安到了。", "安开口了。"],
                    initial_issues=[
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "术语不一致",
                            "suggestion": "统一",
                        }
                    ],
                    review_round=1,
                )
            self.assertEqual(len(calls), 1)  # Only the final turn makes a call.
            self.assertEqual(outcome.issues[0]["candidate_id"], "r1-ch0-base0-candidate0")
            with debug.round_scope(1):
                saved = debug.load_json("agents/r1-chunk-ch0-base0-n2.json")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["status"], "finished")
            self.assertEqual(len(saved["turns"]), 2)
            self.assertIn("evidence_results", saved["turns"][0])

    def test_run_resumes_with_reduced_evidence_rounds_still_finalizes(self):
        """A reduced evidence-round limit must still allow a final call after cached rounds are
        replayed.
        """
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            with debug.round_scope(1):
                evidence_raw = json.dumps(
                    {
                        "action": "request_evidence",
                        "requests": [
                            {
                                "request_id": "term-1",
                                "tool": "term_occurrences",
                                "arguments": {"term": "Ann", "selectors": [1], "context_radius": 0},
                            }
                        ],
                        "complete": False,
                    },
                    ensure_ascii=False,
                )
                turns = []
                for turn_number in (1, 2):
                    turns.append(
                        {
                            "turn": turn_number,
                            "messages": [
                                {"role": "system", "content": "s"},
                                {"role": "user", "content": "u"},
                            ],
                            "status": "responded",
                            "raw_response": evidence_raw,
                            "parsed": json.loads(evidence_raw),
                            "json_repaired": False,
                            "evidence_results": [
                                {
                                    "request_id": "term-1",
                                    "tool": "term_occurrences",
                                    "ok": True,
                                    "occurrences": [],
                                }
                            ],
                        }
                    )
                debug.write_json(
                    "agents/r1-chunk-ch0-base0-n2.json",
                    {
                        "agent_id": "r1-chunk-ch0-base0-n2",
                        "stage": "review.verify",
                        "inference": inference_snapshot(_config().llm, ("review.verify",)),
                        "status": "running",
                        "turns": turns,
                    },
                )
                calls = []

                def handler(m, t, j):
                    calls.append(1)
                    return json.dumps(
                        {
                            "action": "final",
                            "decisions": [
                                {
                                    "candidate_id": "r1-ch0-base0-candidate0",
                                    "index": 0,
                                    "verdict": "confirmed",
                                    "reason": "ok",
                                }
                            ],
                            "complete": True,
                        },
                        ensure_ascii=False,
                    )

                config = _config()
                config.pipeline.review_agent_max_evidence_rounds = (
                    1  # Set a limit below the two cached rounds.
                )
                loop = ReviewAgentLoop(FakeClient(handler=handler), config, self._evidence(), debug)
                outcome = loop.review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived.", "Ann spoke."],
                    targets=["安到了。", "安开口了。"],
                    initial_issues=[
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "术语不一致",
                            "suggestion": "统一",
                        }
                    ],
                    review_round=1,
                )
            # Previously, an empty turn range made no calls and left the trace running forever.
            self.assertEqual(len(calls), 1)  # A final call is required.
            self.assertEqual(outcome.issues[0]["candidate_id"], "r1-ch0-base0-candidate0")
            self.assertFalse(outcome.issues[0].get("agent_fallback"))
            with debug.round_scope(1):
                saved = debug.load_json("agents/r1-chunk-ch0-base0-n2.json")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["status"], "finished")
            self.assertEqual(len(saved["turns"]), 3)

    def test_run_resumes_requesting_turn_without_data(self):
        """Resume an interrupted call whose cached turn is requesting with no response data."""
        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            with debug.round_scope(1):
                debug.write_json(
                    "agents/r1-chunk-ch0-base0-n2.json",
                    {
                        "agent_id": "r1-chunk-ch0-base0-n2",
                        "stage": "review.verify",
                        "inference": inference_snapshot(_config().llm, ("review.verify",)),
                        "status": "running",
                        "turns": [
                            {
                                "turn": 1,
                                "messages": [
                                    {"role": "system", "content": "s"},
                                    {"role": "user", "content": "u"},
                                ],
                                "status": "requesting",
                            }
                        ],
                    },
                )
                calls = []

                def handler(m, t, j):
                    calls.append(1)
                    return json.dumps(
                        {
                            "action": "final",
                            "decisions": [
                                {
                                    "candidate_id": "r1-ch0-base0-candidate0",
                                    "index": 0,
                                    "verdict": "confirmed",
                                    "reason": "ok",
                                }
                            ],
                            "complete": True,
                        },
                        ensure_ascii=False,
                    )

                loop = ReviewAgentLoop(
                    FakeClient(handler=handler), _config(), self._evidence(), debug
                )
                outcome = loop.review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived.", "Ann spoke."],
                    targets=["安到了。", "安开口了。"],
                    initial_issues=[
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "术语不一致",
                            "suggestion": "统一",
                        }
                    ],
                    review_round=1,
                )
            # Reenter the empty requesting turn in place instead of advancing to the next turn.
            self.assertEqual(len(calls), 1)
            self.assertEqual(outcome.issues[0]["candidate_id"], "r1-ch0-base0-candidate0")
            with debug.round_scope(1):
                saved = debug.load_json("agents/r1-chunk-ch0-base0-n2.json")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["status"], "finished")
            self.assertEqual(len(saved["turns"]), 1)

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
            debug = ReviewRunStore(directory)
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
            debug = ReviewRunStore(directory)
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
                ReviewRunStore(directory),
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
                ReviewRunStore(directory),
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
                ReviewRunStore(directory),
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
                ReviewRunStore(directory),
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
                ReviewRunStore(directory),
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
            [GlossaryTerm(source="Ann", target="安", type="person")],
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
                ReviewRunStore(directory),
            ).arbitrate(conflicts[0])

        self.assertEqual(result["status"], "suggested")
        self.assertEqual(result["recommended_value"], "安")
        self.assertEqual(result["supported_issue_ids"], [issue_ids[0]])
        self.assertEqual(result["rejected_issue_ids"], [issue_ids[1]])

    def test_all_issues_with_the_winning_value_are_kept(self):
        """Arbitration chooses a value; retain every issue supporting the winning value."""
        evidence = BookEvidenceIndex(
            [_chapter(0, [("Ann A.", "安。"), ("Ann B.", "安妮。"), ("Ann C.", "安。")])],
            [GlossaryTerm(source="Ann", target="安", type="person")],
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
                ReviewRunStore(directory),
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
                ReviewRunStore(directory),
            ).arbitrate(conflict)

        self.assertEqual(result["recommended_value"], "NASA")

    def test_arbiter_must_requery_inherited_evidence_before_citing_it(self):
        """A block agent's opaque reference does not establish that the arbiter has seen its
        evidence.
        """
        evidence = BookEvidenceIndex(
            [_chapter(0, [("Ann.", "安。"), ("Ann.", "安妮。")])],
            [GlossaryTerm(source="Ann", target="安", type="person")],
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
                ReviewRunStore(directory),
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
                ReviewRunStore(directory),
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
                ReviewRunStore(directory),
            ).arbitrate(conflict)

        self.assertEqual(client.calls, [])
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("size limit", result["reason"])

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
        self.assertEqual(
            final[1]["detail"],
            "Final arbitration requires the expression here to use “安” consistently.",
        )
        self.assertEqual(final[1]["pre_arbitration_detail"], "改写")
        self.assertEqual(
            final[1]["suggestion"],
            "Use “安” consistently for this expression as determined by final arbitration.",
        )
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
            [GlossaryTerm(source="Ann", target="安", type="person")],
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
                ReviewRunStore(directory),
            ).arbitrate(conflict)

        self.assertEqual(result["status"], "unresolved")
        self.assertIn("recommended_value_not_proposed", result["reason"])


if __name__ == "__main__":
    unittest.main()
