"""审校 / 润色 / 回译抽检 测试（离线）。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest

from trans_novel.agents.polisher import Polisher
from trans_novel.agents.reviewer import BackTranslator, Reviewer, ReviewOutputError
from trans_novel.config import Config
from trans_novel.ingest.models import Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore


def _cfg():
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
    })


def _review_response(issues, count):
    return json.dumps({
        "issues": issues,
        "reviewed_segments": count,
        "complete": True,
    }, ensure_ascii=False)


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
        self.assertEqual(client.calls[-1]["tier"], "cheap")  # 审校走廉价档

    def test_reviewer_rejects_invalid_outer_schema(self):
        reviewer = Reviewer(
            FakeClient(handler=lambda m, t, j: json.dumps({"result": []})),
            _cfg(),
        )

        with self.assertRaisesRegex(ReviewOutputError, "completion_footer_not_last"):
            reviewer.review(["あ"], ["甲"])

    def test_reviewer_rejects_bare_issue_array_without_completion_footer(self):
        reviewer = Reviewer(
            FakeClient(handler=lambda m, t, j: json.dumps([{
                "index": 0,
                "type": "missing",
                "detail": "漏译",
                "suggestion": "补译",
            }], ensure_ascii=False)),
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
        response = (
            '{"issues":[],"reviewed_segments":2,"complete":true'
        )
        client = FakeClient(handler=lambda m, t, j: response)
        cfg = _cfg()
        cfg.pipeline.review_concurrency = 1
        orch = Orchestrator(cfg, client=client)

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "state"))
            issues = orch._review_chapter(
                [
                    Segment(index=0, source="源文0", target="译文0"),
                    Segment(index=1, source="源文1", target="译文1"),
                ],
                [],
                store=store,
                chapter_index=3,
            )
            with open(store.event_log_path, "r", encoding="utf-8") as file:
                events = [json.loads(line) for line in file]

        self.assertEqual(issues, [])
        self.assertEqual(len(client.calls), 1)
        repaired = [
            event for event in events
            if event["event"] == "review_json_repaired"
        ]
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

    def test_malformed_chunk_is_recursively_split_and_logged(self):
        def handler(messages, tier, json_mode):
            user = messages[-1]["content"]
            count = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
            if count > 1:
                return '{"issues":['
            return json.dumps({"issues": [{
                "index": 0,
                "type": "missing",
                "detail": "单段恢复成功",
                "suggestion": "补译",
            }],
                "reviewed_segments": count,
                "complete": True,
            }, ensure_ascii=False)

        cfg = _cfg()
        cfg.segment.max_chars_per_batch = 100_000
        cfg.pipeline.review_concurrency = 1
        client = FakeClient(handler=handler)
        orch = Orchestrator(cfg, client=client)
        segments = [
            Segment(index=i, source=f"源文{i}", target=f"译文{i}")
            for i in range(4)
        ]

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "state"))
            issues = orch._review_chapter(
                segments,
                [],
                store=store,
                chapter_index=7,
            )
            with open(store.event_log_path, "r", encoding="utf-8") as file:
                events = [json.loads(line) for line in file]

        self.assertEqual([item["index"] for item in issues], [0, 1, 2, 3])
        self.assertEqual(len(client.calls), 7)  # 4 段二叉拆分：1 + 2 + 4
        splits = [event for event in events if event["event"] == "review_chunk_split"]
        self.assertEqual(len(splits), 3)
        self.assertTrue(all(event["chapter"] == 7 for event in splits))
        self.assertTrue(
            all(
                event["reason"] == "completion_footer_not_last"
                for event in splits
            )
        )
        self.assertTrue(
            all("source" not in event and "target" not in event for event in events)
        )

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

        issues = Orchestrator(cfg, client=client)._review_chapter(
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
            Orchestrator(cfg, client=client)._review_chapter(
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
            return _review_response([{
                "index": 0,
                "type": "missing",
                "detail": detail,
                "suggestion": "补译",
            }], 1)

        cfg = _cfg()
        cfg.segment.max_chars_per_batch = 1  # 审校预算=3，使两个 3 字段落各成一块
        cfg.pipeline.review_concurrency = 2
        orch = Orchestrator(cfg, client=FakeClient(handler=handler))
        segments = [
            Segment(index=0, source="源文甲", target="译文甲"),
            Segment(index=1, source="源文乙", target="译文乙"),
        ]

        issues = orch._review_chapter(segments, [])

        self.assertEqual([it["index"] for it in issues], [0, 1])
        self.assertEqual([it["detail"] for it in issues], ["甲", "乙"])


class TestPolisher(unittest.TestCase):
    def test_polish_ok(self):
        client = FakeClient(handler=lambda m, t, j: json.dumps(
            {"polished": ["润色甲", "润色乙"]}, ensure_ascii=False))
        p = Polisher(client, _cfg())
        out = p.polish(["甲", "乙"])
        self.assertEqual(out, ["润色甲", "润色乙"])
        self.assertEqual(client.calls[-1]["tier"], "strong")

    def test_polish_mismatch_keeps_original(self):
        client = FakeClient(handler=lambda m, t, j: json.dumps(
            {"polished": ["只有一段"]}, ensure_ascii=False))
        p = Polisher(client, _cfg())
        out = p.polish(["甲", "乙"])
        self.assertEqual(out, ["甲", "乙"])  # 段数不符 → 保守保留原译


class TestBackTranslator(unittest.TestCase):
    def test_check(self):
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            if "回译译者" in system:
                return json.dumps({"backtranslations": ["あ", "い"]}, ensure_ascii=False)
            if "保真度" in system:
                return json.dumps({"issues": [{"index": 1, "detail": "含义改变"}]},
                                  ensure_ascii=False)
            return "{}"

        bt = BackTranslator(FakeClient(handler=handler), _cfg())
        issues = bt.check(["あ", "い"], ["甲", "乙"])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["index"], 1)


if __name__ == "__main__":
    unittest.main()
