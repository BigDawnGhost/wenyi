"""Offline LLM abstraction and JSON-parsing tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from trans_novel.llm.json_parser import parse_json_loose, parse_json_result
from trans_novel.llm.providers.fake import FakeClient


class TestParseJsonLoose(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_loose('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(parse_json_loose("```json\n[1,2,3]\n```"), [1, 2, 3])

    def test_surrounded_by_prose(self):
        text = '思考结束。结果如下：["译文1","译文2"] 完毕。'
        self.assertEqual(parse_json_loose(text), ["译文1", "译文2"])

    def test_failure(self):
        with self.assertRaises(ValueError):
            parse_json_loose("没有任何 JSON 内容")


class TestFakeClient(unittest.TestCase):
    def test_default(self):
        c = FakeClient()
        self.assertEqual(
            c.complete([{"role": "user", "content": "x"}], operation="translation.body"), ""
        )
        self.assertEqual(
            c.complete_json([{"role": "user", "content": "x"}], operation="translation.body"), []
        )

    def test_handler(self):
        def handler(messages, tier, json_mode):
            return '["A","B"]' if json_mode else "hello"

        c = FakeClient(handler=handler)
        self.assertEqual(
            c.complete([{"role": "user", "content": "x"}], operation="translation.body"), "hello"
        )
        self.assertEqual(
            c.complete_json([{"role": "user", "content": "x"}], operation="translation.body"),
            ["A", "B"],
        )
        self.assertEqual(len(c.calls), 2)


class TestParseJsonLooseRepairs(unittest.TestCase):
    def test_parse_result_reports_whether_repair_was_used(self):
        self.assertFalse(parse_json_result('{"a": 1}').repaired)
        repaired = parse_json_result('{"a": 1')
        self.assertTrue(repaired.repaired)
        self.assertEqual(repaired.value, {"a": 1})

    def test_inner_ascii_quotes_repaired(self):
        # Regression from a model response containing unescaped English quotation marks.
        raw = '{"translations":["磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。"]}'
        got = parse_json_loose(raw)
        self.assertEqual(
            got["translations"][0], '磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。'
        )

    def test_trailing_extra_brace(self):
        # Regression from a model response with an extra closing brace.
        self.assertEqual(parse_json_loose('{"a": 1}\n}'), {"a": 1})

    def test_unescaped_quotes_with_trailing_extra_brace_keeps_object(self):
        raw = '{"translations":["他说"好"。"]}\n}'
        self.assertEqual(
            parse_json_loose(raw),
            {"translations": ['他说"好"。']},
        )

    def test_valid_json_untouched(self):
        self.assertEqual(parse_json_loose('{"a": "b, c: d"}'), {"a": "b, c: d"})

    def test_escaped_quotes_still_work(self):
        self.assertEqual(parse_json_loose('{"a": "he said \\"hi\\""}'), {"a": 'he said "hi"'})


class TestProviderRequestKwargs(unittest.TestCase):
    messages = [{"role": "user", "content": "x"}]

    def test_json_mode_adds_lowercase_keyword_without_mutating_messages(self):
        from trans_novel.llm.providers._openai_compatible import (
            base_request_kwargs,
        )

        messages = [
            {"role": "system", "content": "仅输出指定对象。"},
            {"role": "user", "content": "x"},
        ]
        kwargs = base_request_kwargs("m", messages, json_mode=True)

        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertIn("json", kwargs["messages"][0]["content"])
        self.assertEqual(messages[0]["content"], "仅输出指定对象。")

    def test_json_mode_also_mentions_json_in_user_message(self):
        # Some gateways check for JSON only in user/input content when forwarding requests,
        # so the final user message also needs an explicit JSON instruction.
        from trans_novel.llm.providers._openai_compatible import (
            base_request_kwargs,
        )

        messages = [
            {"role": "system", "content": "仅输出指定对象。"},
            {"role": "user", "content": "翻译这句话。"},
        ]
        kwargs = base_request_kwargs("m", messages, json_mode=True)

        self.assertIn("json", kwargs["messages"][-1]["content"].lower())
        self.assertEqual(messages[-1]["content"], "翻译这句话。")

    def test_json_mode_skips_user_message_already_mentioning_json(self):
        from trans_novel.llm.providers._openai_compatible import (
            base_request_kwargs,
        )

        messages = [
            {"role": "system", "content": "仅输出指定对象。"},
            {"role": "user", "content": "请输出 JSON 数组。"},
        ]
        kwargs = base_request_kwargs("m", messages, json_mode=True)

        self.assertEqual(kwargs["messages"][-1]["content"], "请输出 JSON 数组。")

    def test_deepseek_dialect_and_recursive_extra_body(self):
        from trans_novel.llm.providers.deepseek import (
            DeepSeekOptions,
            build_request_kwargs,
        )
        from trans_novel.llm.transport import ResolvedModel

        tier = ResolvedModel(
            model="m",
            options=DeepSeekOptions(
                extra_body={"thinking": {"budget": 8192}},
            ),
        )
        kwargs = build_request_kwargs(tier, self.messages)

        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertEqual(
            kwargs["extra_body"],
            {"thinking": {"type": "enabled", "budget": 8192}},
        )

        disabled = ResolvedModel(
            model="m",
            options=DeepSeekOptions(thinking=False),
        )
        disabled_kwargs = build_request_kwargs(disabled, self.messages)
        self.assertNotIn("reasoning_effort", disabled_kwargs)
        self.assertEqual(
            disabled_kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_openrouter_dialect_and_explicit_disable(self):
        from trans_novel.llm.providers.openrouter import (
            OpenRouterOptions,
            build_request_kwargs,
        )
        from trans_novel.llm.transport import ResolvedModel

        enabled = ResolvedModel(
            model="m",
            options=OpenRouterOptions(reasoning_effort="high"),
        )
        disabled = ResolvedModel(
            model="m",
            options=OpenRouterOptions(thinking=False),
        )

        self.assertEqual(
            build_request_kwargs(enabled, self.messages)["extra_body"],
            {"reasoning": {"effort": "high"}},
        )
        self.assertEqual(
            build_request_kwargs(disabled, self.messages)["extra_body"],
            {"reasoning": {"enabled": False}},
        )

    def test_openai_dialect(self):
        from trans_novel.llm.providers.openai import (
            OpenAIOptions,
            build_request_kwargs,
        )
        from trans_novel.llm.transport import ResolvedModel

        tier = ResolvedModel(
            model="m",
            options=OpenAIOptions(reasoning_effort="low"),
        )
        kwargs = build_request_kwargs(tier, self.messages)

        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertNotIn("extra_body", kwargs)

        disabled = ResolvedModel(
            model="m",
            options=OpenAIOptions(thinking=False),
        )
        disabled_kwargs = build_request_kwargs(disabled, self.messages)
        self.assertEqual(disabled_kwargs["reasoning_effort"], "none")

    def test_openai_uses_max_completion_tokens(self):
        from trans_novel.llm.providers.openai import (
            OpenAIOptions,
            build_request_kwargs,
        )
        from trans_novel.llm.transport import ResolvedModel

        enabled = ResolvedModel(model="m", options=OpenAIOptions())
        disabled = ResolvedModel(
            model="m",
            options=OpenAIOptions(thinking=False),
        )

        enabled_kwargs = build_request_kwargs(
            enabled,
            self.messages,
            max_tokens=100,
        )
        disabled_kwargs = build_request_kwargs(
            disabled,
            self.messages,
            max_tokens=100,
        )
        self.assertNotIn("max_tokens", enabled_kwargs)
        self.assertEqual(enabled_kwargs["max_completion_tokens"], 100)
        self.assertNotIn("max_tokens", disabled_kwargs)
        self.assertEqual(disabled_kwargs["max_completion_tokens"], 100)

    def test_generic_compatible_endpoint_maps_reasoning_dialects(self):
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleOptions,
            build_request_kwargs,
        )
        from trans_novel.llm.transport import ResolvedModel

        tier = ResolvedModel(
            model="m",
            options=OpenAICompatibleOptions(
                thinking=True,
                reasoning_effort="medium",
                request_overrides={"thinking": {"budget": 8192}},
            ),
        )
        deepseek = build_request_kwargs(
            tier,
            self.messages,
            max_tokens=100,
            reasoning_style="deepseek",
        )
        openai = build_request_kwargs(
            tier,
            self.messages,
            reasoning_style="openai",
        )
        openrouter = build_request_kwargs(
            tier,
            self.messages,
            reasoning_style="openrouter",
        )

        self.assertEqual(deepseek["reasoning_effort"], "medium")
        self.assertEqual(
            deepseek["extra_body"],
            {"thinking": {"type": "enabled", "budget": 8192}},
        )
        self.assertEqual(deepseek["max_tokens"], 100)
        self.assertEqual(openai["reasoning_effort"], "medium")
        self.assertEqual(
            openai["extra_body"],
            {"thinking": {"budget": 8192}},
        )
        self.assertEqual(
            openrouter["extra_body"],
            {
                "reasoning": {"effort": "medium"},
                "thinking": {"budget": 8192},
            },
        )

    def test_generic_compatible_endpoint_explicitly_disables_reasoning(self):
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleOptions,
            build_request_kwargs,
        )
        from trans_novel.llm.transport import ResolvedModel

        tier = ResolvedModel(
            model="m",
            options=OpenAICompatibleOptions(thinking=False),
        )

        self.assertEqual(
            build_request_kwargs(
                tier,
                self.messages,
                reasoning_style="deepseek",
            )["extra_body"],
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(
            build_request_kwargs(
                tier,
                self.messages,
                reasoning_style="openai",
            )["reasoning_effort"],
            "none",
        )
        self.assertEqual(
            build_request_kwargs(
                tier,
                self.messages,
                reasoning_style="openrouter",
            )["extra_body"],
            {"reasoning": {"enabled": False}},
        )

    def test_generic_compatible_endpoint_can_only_use_raw_overrides(self):
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleOptions,
            build_request_kwargs,
        )
        from trans_novel.llm.transport import ResolvedModel

        tier = ResolvedModel(
            model="m",
            options=OpenAICompatibleOptions(
                thinking=True,
                request_overrides={"enable_thinking": True},
            ),
        )
        kwargs = build_request_kwargs(tier, self.messages, max_tokens=100)

        self.assertNotIn("reasoning_effort", kwargs)
        self.assertEqual(kwargs["extra_body"], {"enable_thinking": True})
        self.assertEqual(kwargs["max_tokens"], 100)


class TestProviderFactory(unittest.TestCase):
    def _config(
        self,
        provider: str,
        *,
        base_url: str | None = None,
        reasoning_style: str | None = None,
    ):
        from trans_novel.config import Config

        connection = {"kind": provider}
        if base_url is not None:
            connection["base_url"] = base_url
        if reasoning_style is not None:
            connection["reasoning_style"] = reasoning_style
        return Config.from_dict(
            {
                "llm": {
                    "providers": {"default": connection},
                    "models": {"m": {"provider": "default", "model": "m"}},
                    "tiers": {tier: "m" for tier in ("strong", "cheap", "fast")},
                }
            }
        )

    def test_builds_each_provider_from_its_own_module(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.openai import OpenAIClient
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleClient,
        )
        from trans_novel.llm.providers.openrouter import OpenRouterClient
        from trans_novel.llm.providers.orcarouter import OrcaRouterClient
        from trans_novel.llm.providers.vllm import VLLMClient

        cases = (
            ("openai", OpenAIClient, None),
            ("openrouter", OpenRouterClient, None),
            ("orcarouter", OrcaRouterClient, None),
            ("openai-compatible", OpenAICompatibleClient, "https://example.test/v1"),
            ("ollama", OllamaClient, None),
            ("vllm", VLLMClient, None),
        )
        for provider, expected_type, base_url in cases:
            with self.subTest(provider=provider):
                self.assertIsInstance(
                    build_client(self._config(provider, base_url=base_url)).adapter("default"),
                    expected_type,
                )

    def test_orcarouter_defaults_and_api_key_validation(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.orcarouter import OrcaRouterClient

        client = build_client(self._config("orcarouter"))
        assert isinstance(client.adapter("default"), OrcaRouterClient)

        self.assertEqual(client.adapter("default").base_url, "https://api.orcarouter.ai/v1")
        self.assertEqual(client.adapter("default").api_key_env, "ORCAROUTER_API_KEY")
        self.assertTrue(client.adapter("default").requires_api_key)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ORCAROUTER_API_KEY"):
                client.validate_credentials()
        with patch.dict(os.environ, {"ORCAROUTER_API_KEY": "secret"}, clear=True):
            client.validate_credentials()

    def test_local_provider_defaults(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.vllm import VLLMClient

        ollama = build_client(self._config("ollama"))
        vllm = build_client(self._config("vllm"))
        assert isinstance(ollama.adapter("default"), OllamaClient)
        assert isinstance(vllm.adapter("default"), VLLMClient)

        self.assertEqual(ollama.adapter("default").base_url, "http://localhost:11434/v1")
        self.assertEqual(vllm.adapter("default").base_url, "http://localhost:8000/v1")
        self.assertFalse(ollama.adapter("default").requires_api_key)
        self.assertFalse(vllm.adapter("default").requires_api_key)

        with patch.dict(os.environ, {}, clear=True):
            ollama.validate_credentials()
            vllm.validate_credentials()

    def test_remote_provider_validates_api_key_before_request(self):
        from trans_novel.llm.factory import build_client

        client = build_client(self._config("deepseek"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY"):
                client.validate_credentials()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret"}, clear=True):
            client.validate_credentials()

    def test_generic_provider_requires_base_url(self):
        from trans_novel.llm.factory import build_client

        with self.assertRaisesRegex(ValueError, "base_url"):
            build_client(self._config("openai-compatible"))

    def test_compatible_clients_use_configured_reasoning_style(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleClient,
        )
        from trans_novel.llm.providers.vllm import VLLMClient

        compatible = build_client(
            self._config(
                "openai-compatible",
                base_url="https://example.test/v1",
                reasoning_style="deepseek",
            )
        )
        ollama = build_client(self._config("ollama", reasoning_style="openai"))
        vllm = build_client(self._config("vllm", reasoning_style="openrouter"))
        compatible_adapter = compatible.adapter("default")
        ollama_adapter = ollama.adapter("default")
        vllm_adapter = vllm.adapter("default")
        assert isinstance(compatible_adapter, OpenAICompatibleClient)
        assert isinstance(ollama_adapter, OllamaClient)
        assert isinstance(vllm_adapter, VLLMClient)

        self.assertEqual(compatible_adapter.reasoning_style, "deepseek")
        self.assertEqual(ollama_adapter.reasoning_style, "openai")
        self.assertEqual(vllm_adapter.reasoning_style, "openrouter")


if __name__ == "__main__":
    unittest.main()
