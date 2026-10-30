"""Review agent evidence-loop and conflict arbitration tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wenyi_core.agents.review_arbiter import ReviewConflictArbiter
from wenyi_core.agents.review_loop import (
    ReviewAgentLoop,
)
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.routing import inference_snapshot
from wenyi_core.pipeline.review_checkpoint import ReviewTraceStore
from wenyi_core.review.conflicts import (
    apply_review_arbitrations,
    build_conflict_groups,
    normalize_review_issues,
)
from wenyi_core.review.evidence import BookEvidenceIndex
from wenyi_core.review.run_store import ReviewRunStore

from .review_fixtures import _chapter, _config, review_evidence


class TestReviewAgentLoop(unittest.TestCase):
    _evidence = staticmethod(review_evidence)

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
                    ReviewTraceStore(debug),
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
                    ReviewTraceStore(debug),
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
                    FakeClient(handler=handler),
                    _config(),
                    self._evidence(),
                    ReviewTraceStore(debug),
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
                    ReviewTraceStore(debug),
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
                    FakeClient(handler=handler),
                    _config(),
                    self._evidence(),
                    ReviewTraceStore(debug),
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
                loop = ReviewAgentLoop(
                    FakeClient(handler=handler), config, self._evidence(), ReviewTraceStore(debug)
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
                    FakeClient(handler=handler),
                    _config(),
                    self._evidence(),
                    ReviewTraceStore(debug),
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

    def test_requests_evidence_then_accepts_complete_before_decisions_and_new_issues(self):
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
                    "complete": True,
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
                },
                ensure_ascii=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            debug = ReviewRunStore(directory)
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                ReviewTraceStore(debug),
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

    def test_complete_before_incomplete_decisions_still_falls_back(self):
        def handler(messages, tier, json_mode):
            return json.dumps(
                {
                    "action": "final",
                    "complete": True,
                    "decisions": [],
                    "new_issues": [],
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                ReviewTraceStore(ReviewRunStore(directory)),
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

        self.assertIn("candidate_decisions_incomplete", outcome.fallback_reason)

    def test_truncated_final_collections_after_complete_still_fall_back(self):
        decision = {
            "candidate_id": "ch0-base0-candidate0",
            "verdict": "confirmed",
            "detail": "候选",
            "suggestion": "补译",
            "reason": "",
            "consistency": {},
            "evidence_refs": [],
        }
        responses = {
            "decisions": '{"action":"final","complete":true,"decisions":[',
            "new_issues": (
                '{"action":"final","complete":true,"decisions":'
                + json.dumps([decision], ensure_ascii=False)
                + ',"new_issues":['
            ),
        }
        initial = {
            "index": 0,
            "type": "missing",
            "detail": "候选",
            "suggestion": "补译",
        }

        for field, raw in responses.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                outcome = ReviewAgentLoop(
                    FakeClient(handler=lambda messages, tier, json_mode: raw),
                    _config(),
                    self._evidence(),
                    ReviewTraceStore(ReviewRunStore(directory)),
                ).review_chunk(
                    chapter=0,
                    chunk_base=0,
                    sources=["Ann arrived."],
                    targets=["安到了。"],
                    initial_issues=[initial],
                )

            self.assertEqual(outcome.fallback_reason, "unsafe_json_repair")

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
                ReviewTraceStore(debug),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                    "complete": True,
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
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            outcome = ReviewAgentLoop(
                FakeClient(handler=handler),
                _config(),
                self._evidence(),
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
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
                ReviewTraceStore(ReviewRunStore(directory)),
            ).arbitrate(conflict)

        self.assertEqual(result["status"], "unresolved")
        self.assertIn("recommended_value_not_proposed", result["reason"])


if __name__ == "__main__":
    unittest.main()
