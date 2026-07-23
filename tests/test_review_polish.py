"""审校 / 润色 / 回译抽检 测试（离线）。"""

from __future__ import annotations

import json
import threading
import unittest

from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryTerm
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

        def handler(messages, tier, json_mode):
            if "术语审查结果的误报复核员" in messages[0]["content"]:
                return json.dumps({"verdicts": [{
                    "candidate_id": 1,
                    "verdict": "confirm",
                    "rationale": "原文人名与对照表一致，译文用了另一人名",
                }]}, ensure_ascii=False)
            return json.dumps(issues, ensure_ascii=False)

        client = FakeClient(handler=handler)
        r = Reviewer(client, _cfg())
        out = r.review(
            ["あ", "綾小路"], ["甲", "另一人"],
            [GlossaryTerm(source="綾小路", target="绫小路", type="人物")],
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(client.calls[-1]["tier"], "cheap")  # 审校走廉价档

    def test_terminology_challenger_rejects_glossary_only_finding(self):
        issues = {"issues": [
            {"index": 0, "type": "missing", "detail": "漏了后半句"},
            {"index": 1, "type": "terminology", "detail": "必须按术语表改名"},
        ]}

        def handler(messages, tier, json_mode):
            if "术语审查结果的误报复核员" in messages[0]["content"]:
                return json.dumps({"verdicts": [{
                    "candidate_id": 1,
                    "verdict": "reject",
                    "rationale": "术语表把两个不同拼写的人名误并为同一人",
                }]}, ensure_ascii=False)
            return json.dumps(issues, ensure_ascii=False)

        client = FakeClient(handler=handler)
        out = Reviewer(client, _cfg()).review(
            ["文A", "Carl entered."], ["译文A", "卡尔走了进来。"],
            [GlossaryTerm(source="Carl", target="卡莱尔", type="人物")],
        )

        self.assertEqual([item["type"] for item in out], ["missing"])
        self.assertEqual(len(client.calls), 2)
        self.assertIn("可错的参考证据", client.calls[1]["messages"][0]["content"])

    def test_invalid_challenger_response_keeps_first_pass_issues(self):
        issues = {"issues": [
            {"index": 0, "type": "terminology", "detail": "译名不一致"},
        ]}

        def handler(messages, tier, json_mode):
            if "术语审查结果的误报复核员" in messages[0]["content"]:
                return json.dumps({"verdicts": []}, ensure_ascii=False)
            return json.dumps(issues, ensure_ascii=False)

        out = Reviewer(FakeClient(handler=handler), _cfg()).review(
            ["綾小路"], ["另一人"],
            [GlossaryTerm(source="綾小路", target="绫小路", type="人物")],
        )

        self.assertEqual(out, issues["issues"])

    def test_review_without_terminology_skips_challenger(self):
        client = FakeClient(handler=lambda m, t, j: json.dumps(
            {"issues": [{"index": 0, "type": "missing", "detail": "漏译"}]},
            ensure_ascii=False))

        out = Reviewer(client, _cfg()).review(["あ"], ["甲"])

        self.assertEqual(len(out), 1)
        self.assertEqual(len(client.calls), 1)

    def test_review_only_sends_terms_found_in_current_source_chunk(self):
        seen_users: list[str] = []

        def handler(messages, tier, json_mode):
            seen_users.append(messages[-1]["content"])
            if "术语审查结果的误报复核员" in messages[0]["content"]:
                return json.dumps({"verdicts": [{
                    "candidate_id": 0,
                    "verdict": "confirm",
                    "rationale": "原文确实出现 Carl，译文违反固定人名译法",
                }]}, ensure_ascii=False)
            return json.dumps({"issues": [{
                "index": 0,
                "type": "terminology",
                "detail": "人名译法不一致",
            }]}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        out = Reviewer(client, _cfg()).review(
            ["Carl entered."],
            ["卡尔走了进来。"],
            [
                GlossaryTerm(source="Carl", target="卡莱尔", type="人物"),
                GlossaryTerm(source="Alice", target="爱丽丝", type="人物"),
            ],
        )

        self.assertEqual(len(out), 1)
        self.assertEqual(len(seen_users), 2)
        self.assertTrue(all("Carl → 卡莱尔" in user for user in seen_users))
        self.assertTrue(all("Alice" not in user for user in seen_users))

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
