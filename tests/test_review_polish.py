"""审校 / 润色 / 回译抽检 测试（离线）。"""

from __future__ import annotations

import json
import threading
import unittest

from trans_novel.config import Config
from trans_novel.ingest.models import Segment
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.agents.reviewer import Reviewer, BackTranslator
from trans_novel.agents.polisher import Polisher
from trans_novel.pipeline.orchestrator import Orchestrator


def _cfg():
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
    })


class TestReviewer(unittest.TestCase):
    def test_review_reports_issues(self):
        issues = {"issues": [
            {"index": 0, "type": "missing", "detail": "漏了后半句"},
            {"index": 1, "type": "terminology", "detail": "人名译法不符"},
        ]}
        client = FakeClient(handler=lambda m, t, j: json.dumps(issues, ensure_ascii=False))
        r = Reviewer(client, _cfg())
        out = r.review(["あ", "い"], ["甲", "乙"])
        self.assertEqual(len(out), 2)
        self.assertEqual(client.calls[-1]["tier"], "cheap")  # 审校走廉价档

    def test_chapter_review_chunks_run_concurrently_and_merge_in_order(self):
        barrier = threading.Barrier(2)

        def handler(messages, tier, json_mode):
            user = messages[1]["content"]
            barrier.wait(timeout=2)
            detail = "甲" if "源文甲" in user else "乙"
            return json.dumps({"issues": [{
                "index": 0,
                "type": "missing",
                "detail": detail,
            }]}, ensure_ascii=False)

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
        self.assertEqual(p.last_failed_indexes, [0, 1])


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
