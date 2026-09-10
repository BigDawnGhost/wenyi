"""Offline review and polishing tests."""

from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from trans_novel.agents.polisher import Polisher
from trans_novel.agents.reviewer import Reviewer, ReviewOutputError
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.ingest.models import Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.review.run_store import ReviewRunStore


def _cfg():
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {
                    "default_strong": {"provider": "default", "model": "p"},
                    "default_cheap": {"provider": "default", "model": "f"},
                },
            },
        }
    )


def _review_response(issues, count):
    return json.dumps(
        {
            "issues": issues,
            "reviewed_segments": count,
            "complete": True,
        },
        ensure_ascii=False,
    )


class TestReviewer(unittest.TestCase):
    def test_review_reports_issues(self):
        issues = [
            {
                "index": 0,
                "type": "missing",
                "detail": "漏了后半句",
                "suggestion": "补译后半句",
            },
            {
                "index": 1,
                "type": "terminology",
                "detail": "人名译法不符",
                "suggestion": "改用术语表译名",
            },
        ]
        client = FakeClient(handler=lambda m, t, j: _review_response(issues, 2))
        r = Reviewer(client, _cfg())
        out = r.review(["あ", "い"], ["甲", "乙"])
        self.assertEqual(len(out), 2)
        self.assertEqual(client.calls[-1]["tier"], "cheap")  # Review uses the cheap tier.
        self.assertIn('"reviewed_segments":2', client.calls[-1]["messages"][0]["content"])

    def test_reviewer_drops_fields_outside_the_initial_issue_contract(self):
        """Cheap initial review cannot bypass the strong agent to inject cross-block
        consistency claims.
        """
        issues = [
            {
                "index": 0,
                "type": "terminology",
                "detail": "人名译法不符",
                "suggestion": "改用术语表译名",
                "consistency": {
                    "kind": "term",
                    "subject_source": "綾小路",
                    "proposed_value": "绫小路",
                },
                "unexpected": "drop me",
            }
        ]
        reviewer = Reviewer(
            FakeClient(handler=lambda m, t, j: _review_response(issues, 1)),
            _cfg(),
        )

        self.assertEqual(
            reviewer.review(["綾小路"], ["绫小路"]),
            [
                {
                    "index": 0,
                    "type": "terminology",
                    "detail": "人名译法不符",
                    "suggestion": "改用术语表译名",
                }
            ],
        )

    def test_reviewer_rejects_invalid_outer_schema(self):
        reviewer = Reviewer(
            FakeClient(handler=lambda m, t, j: json.dumps({"result": []})),
            _cfg(),
        )

        with self.assertRaisesRegex(ReviewOutputError, "completion_footer_not_last"):
            reviewer.review(["あ"], ["甲"])

    def test_reviewer_rejects_bare_issue_array_without_completion_footer(self):
        reviewer = Reviewer(
            FakeClient(
                handler=lambda m, t, j: json.dumps(
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "漏译",
                            "suggestion": "补译",
                        }
                    ],
                    ensure_ascii=False,
                )
            ),
            _cfg(),
        )

        with self.assertRaisesRegex(ReviewOutputError, "response_not_object"):
            reviewer.review(["あ"], ["甲"])

    def test_reviewer_rejects_wrong_completion_receipt(self):
        reviewer = Reviewer(
            FakeClient(handler=lambda m, t, j: _review_response([], 1)),
            _cfg(),
        )

        with self.assertRaisesRegex(ReviewOutputError, "reviewed_segments_mismatch"):
            reviewer.review(["あ", "い"], ["甲", "乙"])

    def test_missing_final_brace_is_repaired_without_another_model_call(self):
        response = '{"issues":[],"reviewed_segments":2,"complete":true'
        client = FakeClient(handler=lambda m, t, j: response)
        cfg = _cfg()
        cfg.pipeline.review_concurrency = 1
        orch = Orchestrator(cfg, client=client)

        with tempfile.TemporaryDirectory() as d:
            debug = ReviewRunStore(d)
            issues = orch._review.review_chapter(
                [
                    Segment(index=0, source="源文0", target="译文0"),
                    Segment(index=1, source="源文1", target="译文1"),
                ],
                [],
                chapter_index=3,
                debug=debug,
            )
            with open(debug.path("events.jsonl"), encoding="utf-8") as file:
                events = [json.loads(line) for line in file]

        self.assertEqual(issues, [])
        self.assertEqual(len(client.calls), 1)
        repaired = [event for event in events if event["event"] == "review_json_repaired"]
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["count"], 2)

    def test_repaired_output_must_still_pass_issue_schema(self):
        response = (
            '{"issues":[{"index":0,"type":"missing","detail":"漏译"}],'
            '"reviewed_segments":1,"complete":true'
        )
        reviewer = Reviewer(
            FakeClient(handler=lambda m, t, j: response),
            _cfg(),
        )

        with self.assertRaisesRegex(
            ReviewOutputError,
            "invalid_issue_suggestion",
        ):
            reviewer.review(["あ"], ["甲"])

    def test_valid_json_with_boolean_index_is_rejected(self):
        reviewer = Reviewer(
            FakeClient(
                handler=lambda m, t, j: _review_response(
                    [
                        {
                            "index": True,
                            "type": "missing",
                            "detail": "错误索引",
                            "suggestion": "补译",
                        }
                    ],
                    1,
                )
            ),
            _cfg(),
        )

        with self.assertRaisesRegex(ReviewOutputError, "invalid_issue_index"):
            reviewer.review(["あ"], ["甲"])

    def test_malformed_chunk_is_recursively_split_and_logged(self):
        def handler(messages, tier, json_mode):
            user = messages[-1]["content"]
            count = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
            if count > 1:
                return '{"issues":['
            return json.dumps(
                {
                    "issues": [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "单段恢复成功",
                            "suggestion": "补译",
                        }
                    ],
                    "reviewed_segments": count,
                    "complete": True,
                },
                ensure_ascii=False,
            )

        cfg = _cfg()
        cfg.segment.max_chars_per_batch = 100_000
        cfg.pipeline.review_concurrency = 1
        client = FakeClient(handler=handler)
        orch = Orchestrator(cfg, client=client)
        segments = [Segment(index=i, source=f"源文{i}", target=f"译文{i}") for i in range(4)]

        with tempfile.TemporaryDirectory() as d:
            debug = ReviewRunStore(d)
            issues = orch._review.review_chapter(
                segments,
                [],
                chapter_index=7,
                debug=debug,
            )
            with open(debug.path("events.jsonl"), encoding="utf-8") as file:
                events = [json.loads(line) for line in file]

        self.assertEqual([item["index"] for item in issues], [0, 1, 2, 3])
        self.assertEqual(
            len(client.calls), 7
        )  # Binary splitting of four paragraphs makes 1 + 2 + 4 calls.
        splits = [event for event in events if event["event"] == "review_chunk_split"]
        self.assertEqual(len(splits), 3)
        self.assertTrue(all(event["chapter"] == 7 for event in splits))
        self.assertTrue(all(event["reason"] == "completion_footer_not_last" for event in splits))
        self.assertTrue(all("source" not in event and "target" not in event for event in events))

    def test_singleton_retries_then_recovers(self):
        attempts = 0

        def handler(messages, tier, json_mode):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return ""
            return _review_response([], 1)

        cfg = _cfg()
        cfg.pipeline.review_output_retries = 2
        client = FakeClient(handler=handler)

        issues = Orchestrator(cfg, client=client)._review.review_chapter(
            [Segment(index=0, source="源文", target="译文")],
            [],
        )

        self.assertEqual(issues, [])
        self.assertEqual(len(client.calls), 3)

    def test_singleton_retry_exhaustion_is_visible(self):
        cfg = _cfg()
        cfg.pipeline.review_output_retries = 2
        client = FakeClient(handler=lambda m, t, j: "")

        with self.assertRaisesRegex(ReviewOutputError, "malformed_json"):
            Orchestrator(cfg, client=client)._review.review_chapter(
                [Segment(index=0, source="源文", target="译文")],
                [],
            )

        self.assertEqual(len(client.calls), 3)

    def test_chapter_review_chunks_run_concurrently_and_merge_in_order(self):
        barrier = threading.Barrier(2)

        def handler(messages, tier, json_mode):
            user = messages[1]["content"]
            barrier.wait(timeout=2)
            detail = "甲" if "源文甲" in user else "乙"
            return _review_response(
                [
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": detail,
                        "suggestion": "补译",
                    }
                ],
                1,
            )

        cfg = _cfg()
        cfg.segment.max_chars_per_batch = (
            1  # A budget of three gives each three-character paragraph its own review block.
        )
        cfg.pipeline.review_concurrency = 2
        orch = Orchestrator(cfg, client=FakeClient(handler=handler))
        segments = [
            Segment(index=0, source="源文甲", target="译文甲"),
            Segment(index=1, source="源文乙", target="译文乙"),
        ]

        issues = orch._review.review_chapter(segments, [])

        self.assertEqual([it["index"] for it in issues], [0, 1])
        self.assertEqual([it["detail"] for it in issues], ["甲", "乙"])

    def test_fresh_review_blocks_share_chapter_glossary_after_cache_lookup(self):
        """A pending block still sees terms from cached neighbors in the same chapter."""
        for scope in ("chapter", "book"):
            for cache_first in (False, True):
                with self.subTest(scope=scope, cache_first=cache_first):
                    cfg = _cfg()
                    cfg.segment.max_chars_per_batch = 1
                    cfg.pipeline.review_concurrency = 2
                    cfg.pipeline.glossary_scope = scope
                    expected_calls = 1 if cache_first else 2
                    barrier = threading.Barrier(expected_calls)

                    def handler(messages, tier, json_mode):
                        barrier.wait(timeout=2)
                        return _review_response([], 1)

                    orch = Orchestrator(cfg, client=FakeClient(handler=handler))
                    segments = [
                        Segment(index=0, source="Ann", target="Anne"),
                        Segment(index=1, source="Bob", target="Robert"),
                    ]
                    terms = [GlossaryTerm(source=s, target=s) for s in ("Ann", "Bob", "Unused")]
                    completed = []
                    with tempfile.TemporaryDirectory() as directory:
                        debug = ReviewRunStore(directory)
                        if cache_first:
                            debug.mark_chunk_done(
                                "r1-ch0-base0-n1",
                                {"issues": [], "initial_issues": [], "dismissed": []},
                            )
                        reviewer = orch._runtime.reviewer
                        with (
                            patch.object(
                                GlossaryStore, "terms_in", wraps=GlossaryStore.terms_in
                            ) as matching,
                            patch.object(
                                reviewer, "review_result", wraps=reviewer.review_result
                            ) as reviewing,
                        ):
                            issues = orch._review.review_chapter(
                                segments,
                                terms,
                                chapter_index=0,
                                review_round=1,
                                debug=debug,
                                on_chunk_finished=completed.append,
                            )

                    self.assertEqual(issues, [])
                    self.assertEqual(completed, [1, 1])
                    self.assertEqual(matching.call_count, 1 if scope == "chapter" else 0)
                    self.assertEqual(reviewing.call_count, expected_calls)
                    expected_terms = terms[:2] if scope == "chapter" else terms
                    for call in reviewing.call_args_list:
                        self.assertEqual(call.args[2], expected_terms)


class TestPolisher(unittest.TestCase):
    def test_polish_ok(self):
        client = FakeClient(
            handler=lambda m, t, j: json.dumps(
                {"polished": ["润色甲", "润色乙"]}, ensure_ascii=False
            )
        )
        p = Polisher(client, _cfg())
        out = p.polish(["甲", "乙"])
        self.assertEqual(out, ["润色甲", "润色乙"])
        self.assertEqual(client.calls[-1]["tier"], "strong")

    def test_polish_mismatch_keeps_original(self):
        client = FakeClient(
            handler=lambda m, t, j: json.dumps({"polished": ["只有一段"]}, ensure_ascii=False)
        )
        p = Polisher(client, _cfg())
        out = p.polish(["甲", "乙"])
        self.assertEqual(
            out, ["甲", "乙"]
        )  # Preserve the original translation on paragraph-count mismatch.


if __name__ == "__main__":
    unittest.main()
