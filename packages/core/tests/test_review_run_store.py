"""Review run artifacts, round isolation and resume-accounting tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wenyi_core.review.models import review_candidate_id
from wenyi_core.review.run_store import ReviewRunStore


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
