"""Polisher batch protocol tests (offline)."""

from __future__ import annotations

import json
import unittest

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.polisher import Polisher, PolishResult
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryTerm
from trans_novel.llm import FakeClient


def _cfg(*, retries: int = 1) -> Config:
    config = Config.from_dict({"llm": fake_llm_dict()})
    config.source_lang = "ja"
    config.pipeline.protocol_retry_limit = retries
    return config


def _response(items: list[dict]) -> str:
    return json.dumps({"polished": items}, ensure_ascii=False)


def _prompt_items(messages: list[dict]) -> list[dict]:
    user = messages[-1]["content"]
    payload = user.split("【待润色段落（JSON）】\n", 1)[1].split("\n\n", 1)[0]
    return json.loads(payload)


class TestPolisher(unittest.TestCase):
    def test_batches_reordered_output_and_preserves_skipped_id_gap(self):
        client = FakeClient(
            handler=lambda *_: _response([{"id": 2, "text": "润色丙"}, {"id": 0, "text": "润色甲"}])
        )

        result = Polisher(client, _cfg()).polish(
            ["甲", "错误字面量", "丙"],
            ["Alpha", "245", "Gamma"],
            style="简洁",
            source_context="前文原文",
            chapter_title="原文标题",
        )

        self.assertEqual(
            result,
            PolishResult(
                ["润色甲", "245", "润色丙"],
                {},
                {0: "润色甲", 2: "润色丙"},
            ),
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["operation"], "polish.batch")
        self.assertEqual(client.calls[0]["agent"], "editor")
        self.assertEqual(
            _prompt_items(client.calls[0]["messages"]),
            [
                {"id": 0, "source": "Alpha", "target": "甲"},
                {"id": 2, "source": "Gamma", "target": "丙"},
            ],
        )

    def test_prompt_system_is_batch_size_independent(self):
        def echo(messages, *_):
            return _response(
                [
                    {"id": item["id"], "text": item["target"] + "润"}
                    for item in _prompt_items(messages)
                ]
            )

        client = FakeClient(handler=echo)
        polisher = Polisher(client, _cfg())
        polisher.polish(["甲"], ["Alpha"])
        polisher.polish(["甲", "乙"], ["Alpha", "Beta"])

        self.assertEqual(
            client.calls[0]["messages"][0]["content"],
            client.calls[1]["messages"][0]["content"],
        )

    def test_all_literals_bypass_editor_and_restore_sources(self):
        client = FakeClient(handler=lambda *_: self.fail("literals must not call LLM"))
        sources = ["{var=a--b}", "245", "258–59", "xix", "xxvi", "xix–xx"]

        result = Polisher(client, _cfg()).polish(["坏译文"] * len(sources), sources, strict=True)

        self.assertEqual(result, PolishResult(sources, {}))
        self.assertEqual(client.calls, [])

    def test_length_mismatch_fails_before_request(self):
        client = FakeClient(handler=lambda *_: self.fail("invalid input must not call LLM"))

        with self.assertRaisesRegex(ValueError, "polish source/target count mismatch"):
            Polisher(client, _cfg()).polish(["甲"], ["Alpha", "Beta"])
        self.assertEqual(client.calls, [])

    def test_non_strict_partial_response_keeps_valid_items(self):
        client = FakeClient(
            handler=lambda *_: _response([{"id": 0, "text": " 润色甲 "}, {"id": 1, "text": "  "}])
        )

        result = Polisher(client, _cfg()).polish(["甲", "乙", "丙"], ["Alpha", "Beta", "Gamma"])

        self.assertEqual(result.texts, ["润色甲", "乙", "丙"])
        self.assertEqual(result.proposals, {0: " 润色甲 "})
        self.assertEqual(
            result.fallback_reasons,
            {1: "polish_item_invalid", 2: "polish_item_missing"},
        )
        self.assertEqual(len(client.calls), 1)

    def test_request_terms_are_detached_before_model_call(self):
        term = GlossaryTerm(
            source="Alice",
            target="爱丽丝",
            aliases=["Al"],
            confidence="high",
            locked=True,
        )

        def mutate_after_prompt(*_):
            term.target = "艾丽斯"
            term.aliases.append("A")
            return _response([{"id": 0, "text": "润色"}])

        result = Polisher(FakeClient(handler=mutate_after_prompt), _cfg()).polish(
            ["译文"], ["Alice"], glossary_terms=[term]
        )

        self.assertEqual(result.locked_terms[0].target, "爱丽丝")
        self.assertEqual(result.locked_terms[0].aliases, ["Al"])
        self.assertIsNot(result.locked_terms[0], term)

    def test_strict_retries_entire_batch_after_partial_response(self):
        responses = iter(
            [
                _response([{"id": 0, "text": "首次甲"}]),
                _response([{"id": 1, "text": "最终乙"}, {"id": 0, "text": "最终甲"}]),
            ]
        )
        client = FakeClient(handler=lambda *_: next(responses))

        result = Polisher(client, _cfg(retries=1)).polish(
            ["甲", "乙"], ["Alpha", "Beta"], strict=True
        )

        self.assertEqual(
            result,
            PolishResult(["最终甲", "最终乙"], {}, {0: "最终甲", 1: "最终乙"}),
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(
            _prompt_items(client.calls[0]["messages"]),
            _prompt_items(client.calls[1]["messages"]),
        )

    def test_strict_exhaustion_uses_only_last_partial_attempt(self):
        responses = iter(
            [
                _response([{"id": 0, "text": "早先甲"}]),
                _response([{"id": 1, "text": "最终乙"}]),
            ]
        )
        client = FakeClient(handler=lambda *_: next(responses))

        result = Polisher(client, _cfg(retries=1)).polish(
            ["甲", "乙"], ["Alpha", "Beta"], strict=True
        )

        self.assertEqual(result.texts, ["甲", "最终乙"])
        self.assertEqual(result.fallback_reasons, {0: "polish_item_missing"})
        self.assertEqual(result.proposals, {1: "最终乙"})
        self.assertEqual(len(client.calls), 2)

    def test_invalid_final_attempt_erases_earlier_partial(self):
        responses = iter([_response([{"id": 0, "text": "早先甲"}]), "not json"])
        client = FakeClient(handler=lambda *_: next(responses))

        result = Polisher(client, _cfg(retries=1)).polish(
            ["甲", "乙"], ["Alpha", "Beta"], strict=True
        )

        self.assertEqual(result.texts, ["甲", "乙"])
        self.assertEqual(
            result.fallback_reasons,
            {0: "polish_batch_invalid", 1: "polish_batch_invalid"},
        )
        self.assertEqual(result.proposals, {})
        self.assertEqual(len(client.calls), 2)

    def test_batch_shape_and_id_violations_reject_every_eligible_item(self):
        invalid_responses = [
            {"polished": [{"id": 0, "text": "甲"}, {"id": 0, "text": "重复"}]},
            {"polished": [{"id": 3, "text": "未知"}]},
            {"polished": [{"id": True, "text": "布尔"}]},
            {"polished": ["不是对象"]},
            {"polished": "不是列表"},
            [],
        ]
        for response in invalid_responses:
            with self.subTest(response=response):
                client = FakeClient(handler=lambda *_, value=response: json.dumps(value))
                result = Polisher(client, _cfg()).polish(["甲", "乙"], ["Alpha", "Beta"])
                self.assertEqual(result.texts, ["甲", "乙"])
                self.assertEqual(
                    result.fallback_reasons,
                    {0: "polish_batch_invalid", 1: "polish_batch_invalid"},
                )
                self.assertEqual(len(client.calls), 1)

    def test_provider_and_program_errors_propagate(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                client = FakeClient(
                    handler=lambda *_: (_ for _ in ()).throw(RuntimeError("provider failed"))
                )
                with self.assertRaisesRegex(RuntimeError, "provider failed"):
                    Polisher(client, _cfg()).polish(["甲"], ["Alpha"], strict=strict)
                self.assertEqual(len(client.calls), 1)
