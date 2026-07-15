"""LLM 抽象层与 JSON 解析的测试（离线）。"""

from __future__ import annotations

import unittest

from trans_novel.llm.json_parser import parse_json_loose
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


class TestResolveTier(unittest.TestCase):
    def test_fallback_chain(self):
        from trans_novel.config import TierConfig
        from trans_novel.llm.tiers import resolve_tier

        strong = TierConfig(model="pro")
        cheap = TierConfig(model="flash")
        fast = TierConfig(model="flash", options={"thinking": False})

        # 三档全有 → 各归各
        tiers = {"strong": strong, "cheap": cheap, "fast": fast}
        self.assertIs(resolve_tier(tiers, "fast"), fast)
        self.assertIs(resolve_tier(tiers, "cheap"), cheap)
        self.assertIs(resolve_tier(tiers, "strong"), strong)
        # 无 fast → 落 cheap（不升到更贵的 strong）
        tiers2 = {"strong": strong, "cheap": cheap}
        self.assertIs(resolve_tier(tiers2, "fast"), cheap)
        # 只有 strong → 都落 strong
        tiers3 = {"strong": strong}
        self.assertIs(resolve_tier(tiers3, "fast"), strong)
        self.assertIs(resolve_tier(tiers3, "cheap"), strong)
        # 未知档 → 落 strong
        self.assertIs(resolve_tier(tiers, "unknown"), strong)


class TestFakeClient(unittest.TestCase):
    def test_default(self):
        c = FakeClient()
        self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "")
        self.assertEqual(c.complete_json([{"role": "user", "content": "x"}]), [])

    def test_handler(self):
        def handler(messages, tier, json_mode):
            return '["A","B"]' if json_mode else "hello"

        c = FakeClient(handler=handler)
        self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "hello")
        self.assertEqual(c.complete_json([{"role": "user", "content": "x"}]), ["A", "B"])
        self.assertEqual(len(c.calls), 2)


class TestParseJsonLooseRepairs(unittest.TestCase):
    def test_inner_ascii_quotes_repaired(self):
        # 真实案例：claude-opus-4.6 经 OpenRouter 输出的译文含未转义英文引号
        raw = '{"translations":["磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。"]}'
        got = parse_json_loose(raw)
        self.assertEqual(got["translations"][0], '磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。')

    def test_trailing_extra_brace(self):
        # 真实案例：gemini-3.1-pro 输出末尾多一个 }
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

    def test_deepseek_dialect_and_recursive_extra_body(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.deepseek import (
            DeepSeekTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=DeepSeekTierOptions(
                extra_body={"thinking": {"budget": 8192}},
            ),
        )
        kwargs = build_request_kwargs(tier, self.messages)

        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertEqual(
            kwargs["extra_body"],
            {"thinking": {"type": "enabled", "budget": 8192}},
        )

        disabled = ResolvedTier(
            model="m",
            options=DeepSeekTierOptions(thinking=False),
        )
        disabled_kwargs = build_request_kwargs(disabled, self.messages)
        self.assertNotIn("reasoning_effort", disabled_kwargs)
        self.assertEqual(
            disabled_kwargs["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_openrouter_dialect_and_explicit_disable(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openrouter import (
            OpenRouterTierOptions,
            build_request_kwargs,
        )

        enabled = ResolvedTier(
            model="m",
            options=OpenRouterTierOptions(reasoning_effort="high"),
        )
        disabled = ResolvedTier(
            model="m",
            options=OpenRouterTierOptions(thinking=False),
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
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai import (
            OpenAITierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAITierOptions(reasoning_effort="low"),
        )
        kwargs = build_request_kwargs(tier, self.messages)

        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertNotIn("extra_body", kwargs)

        disabled = ResolvedTier(
            model="m",
            options=OpenAITierOptions(thinking=False),
        )
        disabled_kwargs = build_request_kwargs(disabled, self.messages)
        self.assertEqual(disabled_kwargs["reasoning_effort"], "none")

    def test_openai_uses_max_completion_tokens(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai import (
            OpenAITierOptions,
            build_request_kwargs,
        )

        enabled = ResolvedTier(model="m", options=OpenAITierOptions())
        disabled = ResolvedTier(
            model="m",
            options=OpenAITierOptions(thinking=False),
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
        self.assertEqual(enabled_kwargs["max_completion_tokens"], 4096)
        self.assertNotIn("max_tokens", disabled_kwargs)
        self.assertEqual(disabled_kwargs["max_completion_tokens"], 100)

    def test_generic_compatible_endpoint_maps_reasoning_dialects(self):
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAICompatibleTierOptions(
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
        self.assertEqual(deepseek["max_tokens"], 4096)
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
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAICompatibleTierOptions(thinking=False),
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
        from trans_novel.llm.providers._openai_compatible import ResolvedTier
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleTierOptions,
            build_request_kwargs,
        )

        tier = ResolvedTier(
            model="m",
            options=OpenAICompatibleTierOptions(
                thinking=True,
                request_overrides={"enable_thinking": True},
            ),
        )
        kwargs = build_request_kwargs(tier, self.messages, max_tokens=100)

        self.assertNotIn("reasoning_effort", kwargs)
        self.assertEqual(kwargs["extra_body"], {"enable_thinking": True})
        self.assertEqual(kwargs["max_tokens"], 4096)


class TestProviderFactory(unittest.TestCase):
    def _config(
        self,
        provider: str,
        *,
        base_url: str | None = None,
        reasoning_style: str | None = None,
    ):
        from trans_novel.config import Config

        llm = {
            "provider": provider,
            "tiers": {"strong": {"model": "m"}},
        }
        if base_url is not None:
            llm["base_url"] = base_url
        if reasoning_style is not None:
            llm["reasoning_style"] = reasoning_style
        return Config.from_dict({"llm": llm})

    def test_builds_each_provider_from_its_own_module(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.openai import OpenAIClient
        from trans_novel.llm.providers.openai_compatible import (
            OpenAICompatibleClient,
        )
        from trans_novel.llm.providers.openrouter import OpenRouterClient
        from trans_novel.llm.providers.vllm import VLLMClient

        cases = (
            ("openai", OpenAIClient, None),
            ("openrouter", OpenRouterClient, None),
            ("openai-compatible", OpenAICompatibleClient, "https://example.test/v1"),
            ("ollama", OllamaClient, None),
            ("vllm", VLLMClient, None),
        )
        for provider, expected_type, base_url in cases:
            with self.subTest(provider=provider):
                self.assertIsInstance(
                    build_client(self._config(provider, base_url=base_url)),
                    expected_type,
                )

    def test_local_provider_defaults(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.ollama import OllamaClient
        from trans_novel.llm.providers.vllm import VLLMClient

        ollama = build_client(self._config("ollama"))
        vllm = build_client(self._config("vllm"))
        assert isinstance(ollama, OllamaClient)
        assert isinstance(vllm, VLLMClient)

        self.assertEqual(ollama.base_url, "http://localhost:11434/v1")
        self.assertEqual(vllm.base_url, "http://localhost:8000/v1")
        self.assertFalse(ollama.requires_api_key)
        self.assertFalse(vllm.requires_api_key)

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
        assert isinstance(compatible, OpenAICompatibleClient)
        assert isinstance(ollama, OllamaClient)
        assert isinstance(vllm, VLLMClient)

        self.assertEqual(compatible.reasoning_style, "deepseek")
        self.assertEqual(ollama.reasoning_style, "openai")
        self.assertEqual(vllm.reasoning_style, "openrouter")


if __name__ == "__main__":
    unittest.main()
