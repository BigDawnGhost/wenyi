"""Polisher behavior tests (offline)."""

from __future__ import annotations

import json
import unittest

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.polisher import Polisher
from trans_novel.config import Config
from trans_novel.llm import FakeClient


def _cfg():
    config = Config.from_dict({"llm": fake_llm_dict()})
    config.source_lang = "ja"
    return config


class TestPolisher(unittest.TestCase):
    def test_polishes_each_segment_independently(self):
        responses = iter(["润色甲", "润色乙"])
        client = FakeClient(
            handler=lambda m, a, o, j: json.dumps(
                {"polished": [next(responses)]}, ensure_ascii=False
            )
        )

        out = Polisher(client, _cfg()).polish(["甲", "乙"], ["a", "b"])

        self.assertEqual(out, ["润色甲", "润色乙"])
        self.assertEqual([call["operation"] for call in client.calls], ["polish.segment"] * 2)
        self.assertTrue(all(call["agent"] == "editor" for call in client.calls))

    def test_machine_and_page_literals_restore_source_and_skip_editor(self):
        client = FakeClient(handler=lambda *args: self.fail("machine literals must not call LLM"))

        sources = ["{var=a--b}", "245", "258–59", "xix", "xxvi", "xix–xx"]
        self.assertEqual(
            Polisher(client, _cfg()).polish(["坏译文"] * len(sources), sources, strict=True),
            sources,
        )
        self.assertEqual(client.calls, [])

    def test_polish_uses_plain_text_contract(self):
        client = FakeClient(
            handler=lambda m, a, o, j: json.dumps({"polished": ["润甲乙"]}, ensure_ascii=False)
        )
        polished = Polisher(client, _cfg()).polish(
            ["甲乙"],
            ["Alpha Beta"],
            strict=True,
        )
        self.assertEqual(polished, ["润甲乙"])
        self.assertNotIn("EPUB", client.calls[0]["messages"][-1]["content"])

    def test_invalid_non_strict_item_keeps_only_its_original(self):
        responses = iter([{"polished": []}, {"polished": ["润色乙"]}])
        client = FakeClient(
            handler=lambda m, a, o, j: json.dumps(next(responses), ensure_ascii=False)
        )

        out = Polisher(client, _cfg()).polish(["甲", "乙"], ["a", "b"])

        self.assertEqual(out, ["甲", "润色乙"])

    def test_invalid_strict_item_keeps_original_and_continues(self):
        responses = iter(
            [
                {"polished": ["润色甲"]},
                {"polished": []},
                {"polished": []},
                {"polished": []},
                {"polished": ["润色丙"]},
            ]
        )
        client = FakeClient(
            handler=lambda m, a, o, j: json.dumps(next(responses), ensure_ascii=False)
        )

        self.assertEqual(
            Polisher(client, _cfg()).polish(["甲", "乙", "丙"], ["a", "b", "c"], strict=True),
            ["润色甲", "乙", "润色丙"],
        )
        self.assertEqual(len(client.calls), 5)

    def test_invalid_strict_item_retries(self):
        responses = iter([{"polished": []}, {"polished": ["润色甲"]}])
        client = FakeClient(
            handler=lambda m, a, o, j: json.dumps(next(responses), ensure_ascii=False)
        )

        self.assertEqual(
            Polisher(client, _cfg()).polish(["甲"], ["a"], strict=True),
            ["润色甲"],
        )
        self.assertEqual(len(client.calls), 2)

    def test_each_prompt_contains_only_its_source(self):
        client = FakeClient(
            handler=lambda m, a, o, j: json.dumps({"polished": ["润色"]}, ensure_ascii=False)
        )

        Polisher(client, _cfg()).polish(["甲", "乙"], sources=["ALPHA_SRC", "BETA_SRC"], style="S")

        first = client.calls[0]["messages"][-1]["content"]
        second = client.calls[1]["messages"][-1]["content"]
        self.assertIn("ALPHA_SRC", first)
        self.assertNotIn("BETA_SRC", first)
        self.assertIn("BETA_SRC", second)
        self.assertNotIn("ALPHA_SRC", second)

    def test_source_context_is_segment_local_and_does_not_read_neighbor_targets(self):
        client = FakeClient(
            handler=lambda m, a, o, j: json.dumps({"polished": ["润色"]}, ensure_ascii=False)
        )
        Polisher(client, _cfg()).polish(
            ["TARGET_ALPHA", "TARGET_BETA"],
            ["SOURCE_ALPHA", "SOURCE_BETA"],
            source_contexts=("PRECEDING_FIRST", "PRECEDING_SECOND"),
            chapter_title="ORIGINAL_CHAPTER",
        )
        first, second = [call["messages"][-1]["content"] for call in client.calls]
        self.assertIn("PRECEDING_FIRST", first)
        self.assertNotIn("PRECEDING_SECOND", first)
        self.assertNotIn("TARGET_BETA", first)
        self.assertIn("PRECEDING_SECOND", second)
        self.assertNotIn("PRECEDING_FIRST", second)
        self.assertNotIn("TARGET_ALPHA", second)
        for prompt in (first, second):
            self.assertIn("ORIGINAL_CHAPTER", prompt)
            self.assertNotIn("{source_context}", prompt)
            self.assertNotIn("{chapter_title}", prompt)

    def test_source_context_count_mismatch_fails_before_requests(self):
        client = FakeClient(handler=lambda *args: self.fail("must validate before requests"))
        with self.assertRaises(ValueError):
            Polisher(client, _cfg()).polish(
                ["TARGET_ALPHA", "TARGET_BETA"],
                ["SOURCE_ALPHA", "SOURCE_BETA"],
                source_contexts=("ONE_CONTEXT",),
            )
        self.assertEqual(client.calls, [])
