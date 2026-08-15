"""Anthropic / OpenAI 双格式通用 Provider 的离线单元测试。"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from trans_novel.config import LLMConfig, TierConfig
from trans_novel.llm.providers.universal import (
    ANTHROPIC_DEFAULT_MAX_TOKENS,
    UniversalClient,
    convert_messages_to_anthropic,
    extract_anthropic_text,
    extract_openai_text,
    normalize_anthropic_usage,
    normalize_base_url,
    normalize_openai_usage,
    resolve_request_config,
)


def _config(api_format: str = "openai", **overrides) -> LLMConfig:
    values = {
        "api_format": api_format,
        "api_key": "secret",
        "base_url": "https://example.test/v1",
        "model": "global-model",
    }
    values.update(overrides)
    return LLMConfig(**values)


class TestURLNormalization(unittest.TestCase):
    def test_accepts_base_and_complete_openai_urls(self):
        self.assertEqual(
            normalize_base_url("https://example.test/api/v1/", "openai"),
            "https://example.test/api/v1",
        )
        self.assertEqual(
            normalize_base_url(
                "https://example.test/api/v1/chat/completions/",
                "openai",
            ),
            "https://example.test/api/v1",
        )

    def test_accepts_base_and_complete_anthropic_urls(self):
        self.assertEqual(
            normalize_base_url("https://example.test/proxy", "anthropic"),
            "https://example.test/proxy",
        )
        self.assertEqual(
            normalize_base_url("https://example.test/v1/", "anthropic"),
            "https://example.test",
        )
        self.assertEqual(
            normalize_base_url("https://example.test/proxy/v1/", "anthropic"),
            "https://example.test/proxy",
        )
        self.assertEqual(
            normalize_base_url(
                "https://example.test/proxy/v1/messages/",
                "anthropic",
            ),
            "https://example.test/proxy",
        )

    def test_rejects_invalid_scheme_query_and_fragment(self):
        for value in (
            "ftp://example.test/v1",
            "not-a-url",
            "https://example.test/v1?token=x",
            "https://example.test/v1#fragment",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "base_url"):
                normalize_base_url(value, "openai")


class TestTierResolution(unittest.TestCase):
    def test_global_model_and_options_apply_to_every_unconfigured_tier(self):
        cfg = _config(
            max_tokens=1000,
            thinking=True,
            request_overrides={"metadata": {"source": "global", "keep": True}},
        )

        for tier in ("strong", "cheap", "fast"):
            with self.subTest(tier=tier):
                resolved = resolve_request_config(cfg, tier)
                self.assertEqual(resolved.model, "global-model")
                self.assertEqual(resolved.max_tokens, 1000)
                self.assertTrue(resolved.thinking)

    def test_unknown_tier_is_not_allowed_to_use_the_global_model(self):
        with self.assertRaisesRegex(ValueError, "未知 LLM tier：fasat"):
            resolve_request_config(_config(), "fasat")

    def test_tier_is_an_exact_override_and_deep_merges_raw_body(self):
        cfg = _config(
            thinking=True,
            request_overrides={"metadata": {"source": "global", "keep": True}},
            tiers={
                "fast": TierConfig(
                    model="fast-model",
                    thinking=False,
                    request_overrides={"metadata": {"source": "tier"}},
                )
            },
        )

        fast = resolve_request_config(cfg, "fast")
        cheap = resolve_request_config(cfg, "cheap")
        self.assertEqual(fast.model, "fast-model")
        self.assertFalse(fast.thinking)
        self.assertEqual(
            fast.request_overrides,
            {"metadata": {"source": "tier", "keep": True}},
        )
        self.assertEqual(cheap.model, "global-model")
        self.assertTrue(cheap.thinking)


class TestMessageAndUsageHelpers(unittest.TestCase):
    def test_anthropic_system_messages_are_merged_and_roles_converted(self):
        system, messages = convert_messages_to_anthropic(
            [
                {"role": "system", "content": "first"},
                {"role": "system", "content": "second"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a"},
                {"role": "tool", "content": "fallback"},
            ]
        )

        self.assertEqual(system, "first\n\nsecond")
        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "fallback"},
            ],
        )

    def test_anthropic_conversion_keeps_assistant_text_when_tool_calls_exist(self):
        _system, messages = convert_messages_to_anthropic(
            [
                {
                    "role": "assistant",
                    "content": "keep this answer",
                    "tool_calls": [{"id": "call-1"}],
                }
            ]
        )

        self.assertEqual(
            messages,
            [{"role": "assistant", "content": "keep this answer"}],
        )

    def test_openai_usage_normalizes_nested_cache_tokens(self):
        sample = normalize_openai_usage(
            SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                prompt_tokens_details=SimpleNamespace(cached_tokens=40),
            )
        )
        assert sample is not None
        self.assertEqual(sample.cache_hit_tokens, 40)
        self.assertEqual(sample.cache_miss_tokens, 60)

    def test_openai_usage_without_cache_detail_keeps_cache_unknown(self):
        sample = normalize_openai_usage(
            SimpleNamespace(
                prompt_tokens=7,
                completion_tokens=3,
                total_tokens=10,
            )
        )
        assert sample is not None
        self.assertEqual(sample.cache_hit_tokens, 0)
        self.assertEqual(sample.cache_miss_tokens, 0)

    def test_anthropic_usage_sums_all_input_categories(self):
        sample = normalize_anthropic_usage(
            SimpleNamespace(
                input_tokens=10,
                cache_creation_input_tokens=20,
                cache_read_input_tokens=30,
                output_tokens=5,
            )
        )
        assert sample is not None
        self.assertEqual(sample.prompt_tokens, 60)
        self.assertEqual(sample.completion_tokens, 5)
        self.assertEqual(sample.total_tokens, 65)
        self.assertEqual(sample.cache_hit_tokens, 30)
        self.assertEqual(sample.cache_miss_tokens, 30)

    def test_response_extractors_only_return_text_blocks(self):
        openai_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=[
                            {"type": "text", "text": "A"},
                            {"type": "tool", "text": "ignored"},
                            SimpleNamespace(type="text", text="B"),
                        ]
                    )
                )
            ]
        )
        anthropic_response = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="hidden"),
                SimpleNamespace(type="text", text="C"),
                {"type": "text", "text": "D"},
            ]
        )
        self.assertEqual(extract_openai_text(openai_response), "AB")
        self.assertEqual(extract_anthropic_text(anthropic_response), "CD")


class TestRequestConstruction(unittest.TestCase):
    messages = [
        {"role": "system", "content": "Only the result."},
        {"role": "user", "content": "Translate."},
    ]

    def test_openai_json_mode_thinking_and_runtime_limit(self):
        client = UniversalClient(
            _config(
                thinking=True,
                reasoning_effort="medium",
                max_tokens=1000,
                max_tokens_field="max_completion_tokens",
                temperature=0.2,
                request_overrides={"metadata": {"source": "wenyi"}},
            )
        )
        settings = resolve_request_config(client.cfg, "strong")
        kwargs = client._build_openai_kwargs(
            settings,
            self.messages,
            json_mode=True,
            max_tokens=600,
        )

        self.assertEqual(kwargs["max_completion_tokens"], 600)
        self.assertNotIn("max_tokens", kwargs)
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertIn("json", kwargs["messages"][0]["content"].lower())
        self.assertIn("json", kwargs["messages"][-1]["content"].lower())
        self.assertEqual(self.messages[0]["content"], "Only the result.")
        self.assertEqual(kwargs["temperature"], 0.2)
        self.assertEqual(kwargs["reasoning_effort"], "medium")
        self.assertEqual(kwargs["extra_body"], {"metadata": {"source": "wenyi"}})

    def test_openai_thinking_disable_and_raw_response_format_protection(self):
        client = UniversalClient(
            _config(
                thinking=False,
                request_overrides={"response_format": {"type": "text"}},
            )
        )
        kwargs = client._build_openai_kwargs(
            resolve_request_config(client.cfg, "strong"),
            self.messages,
            json_mode=True,
            max_tokens=None,
        )
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(kwargs["reasoning_effort"], "none")
        self.assertNotIn("extra_body", kwargs)

    def test_openai_uses_default_token_field_and_default_thinking_effort(self):
        client = UniversalClient(_config(thinking=True, max_tokens=512))
        kwargs = client._build_openai_kwargs(
            resolve_request_config(client.cfg, "cheap"),
            self.messages,
            json_mode=False,
            max_tokens=None,
        )

        self.assertEqual(kwargs["max_tokens"], 512)
        self.assertNotIn("max_completion_tokens", kwargs)
        self.assertEqual(kwargs["reasoning_effort"], "high")

    def test_anthropic_defaults_limit_and_maps_adaptive_thinking(self):
        client = UniversalClient(
            _config(
                api_format="anthropic",
                thinking=True,
                reasoning_effort="low",
                temperature=0.1,
            )
        )
        kwargs = client._build_anthropic_kwargs(
            resolve_request_config(client.cfg, "strong"),
            self.messages,
            json_mode=True,
            max_tokens=None,
        )

        self.assertEqual(kwargs["max_tokens"], ANTHROPIC_DEFAULT_MAX_TOKENS)
        self.assertIn("json", kwargs["system"].lower())
        self.assertIn("json", kwargs["messages"][-1]["content"].lower())
        self.assertFalse(kwargs["stream"])
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertEqual(kwargs["thinking"], {"type": "adaptive"})
        self.assertEqual(kwargs["output_config"], {"effort": "low"})
        self.assertNotIn("extra_body", kwargs)
        self.assertNotIn("response_format", kwargs)

    def test_anthropic_manual_thinking_override_replaces_adaptive_mode(self):
        client = UniversalClient(
            _config(
                api_format="anthropic",
                thinking=True,
                reasoning_effort="high",
                request_overrides={
                    "thinking": {"type": "enabled", "budget_tokens": 2048},
                    "output_config": {"custom": True},
                },
            )
        )
        kwargs = client._build_anthropic_kwargs(
            resolve_request_config(client.cfg, "strong"),
            self.messages,
            json_mode=False,
            max_tokens=3000,
        )
        self.assertEqual(kwargs["max_tokens"], 3000)
        self.assertEqual(
            kwargs["thinking"],
            {"type": "enabled", "budget_tokens": 2048},
        )
        self.assertEqual(
            kwargs["output_config"],
            {"effort": "high", "custom": True},
        )
        self.assertNotIn("extra_body", kwargs)

    def test_anthropic_maps_disabled_thinking_and_configured_limit(self):
        client = UniversalClient(_config(api_format="anthropic", thinking=False, max_tokens=1024))
        kwargs = client._build_anthropic_kwargs(
            resolve_request_config(client.cfg, "fast"),
            self.messages,
            json_mode=False,
            max_tokens=None,
        )

        self.assertEqual(kwargs["max_tokens"], 1024)
        self.assertEqual(kwargs["thinking"], {"type": "disabled"})

    def test_reserved_overrides_are_rejected(self):
        for api_format, overrides in (
            ("openai", {"messages": []}),
            ("openai", {"max_tokens": 1}),
            ("anthropic", {"system": "replace"}),
            ("anthropic", {"response_format": {"type": "json_object"}}),
        ):
            with (
                self.subTest(api_format=api_format),
                self.assertRaisesRegex(ValueError, "保留字段"),
            ):
                UniversalClient(_config(api_format=api_format, request_overrides=overrides))

    def test_reserved_tier_override_reports_its_location(self):
        with self.assertRaisesRegex(ValueError, r"llm\.tiers\.fast\.request_overrides"):
            UniversalClient(_config(tiers={"fast": TierConfig(request_overrides={"stream": True})}))

    def test_anthropic_rejects_openai_max_token_field(self):
        with self.assertRaisesRegex(ValueError, "仅支持 max_tokens_field"):
            UniversalClient(
                _config(
                    api_format="anthropic",
                    max_tokens_field="max_completion_tokens",
                )
            )


class TestSDKAndCompletion(unittest.TestCase):
    def test_direct_key_precedes_environment_and_is_redacted(self):
        cfg = _config(api_key="direct-secret", api_key_env="TEST_LLM_KEY")
        client = UniversalClient(cfg)
        with patch.dict(os.environ, {"TEST_LLM_KEY": "environment-secret"}, clear=True):
            self.assertEqual(client._api_key(), "direct-secret")
        self.assertNotIn("direct-secret", repr(cfg))

    def test_environment_key_is_validated_without_leaking_values(self):
        client = UniversalClient(_config(api_key=None, api_key_env="TEST_LLM_KEY"))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TEST_LLM_KEY") as raised:
                client.validate_credentials()
        self.assertNotIn("secret", str(raised.exception))
        with patch.dict(os.environ, {"TEST_LLM_KEY": "secret"}, clear=True):
            client.validate_credentials()
            self.assertEqual(client._api_key(), "secret")

    def test_openai_sdk_is_lazy_singleton_with_internal_retries_disabled(self):
        client = UniversalClient(_config())
        sdk = MagicMock()
        with patch("openai.OpenAI", return_value=sdk) as sdk_type:
            self.assertIs(client._ensure_client(), sdk)
            self.assertIs(client._ensure_client(), sdk)
        sdk_type.assert_called_once_with(
            api_key="secret",
            base_url="https://example.test/v1",
            timeout=600,
            max_retries=0,
        )

    def test_anthropic_sdk_is_lazy_singleton_with_internal_retries_disabled(self):
        client = UniversalClient(_config(api_format="anthropic"))
        sdk = MagicMock()
        with patch("anthropic.Anthropic", return_value=sdk) as sdk_type:
            self.assertIs(client._ensure_client(), sdk)
            self.assertIs(client._ensure_client(), sdk)
        sdk_type.assert_called_once_with(
            api_key="secret",
            base_url="https://example.test",
            timeout=600,
            max_retries=0,
        )

    def test_openai_complete_records_usage_and_activity(self):
        client = UniversalClient(_config())
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="done"))],
            usage=SimpleNamespace(
                prompt_tokens=4,
                completion_tokens=2,
                total_tokens=6,
                prompt_tokens_details=SimpleNamespace(cached_tokens=1),
            ),
        )
        client._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=MagicMock(return_value=response))
            )
        )
        events = []
        client.set_activity_sink(lambda event, **data: events.append({"event": event, **data}))

        self.assertEqual(
            client.complete(
                [{"role": "user", "content": "x"}],
                tier="cheap",
                stage="Translator",
            ),
            "done",
        )
        self.assertEqual(
            [event["event"] for event in events],
            ["request_started", "request_finished"],
        )
        self.assertEqual(client.usage_summary()["totals"]["total_tokens"], 6)

    def test_anthropic_complete_joins_text_and_records_usage(self):
        client = UniversalClient(_config(api_format="anthropic"))
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="hidden"),
                SimpleNamespace(type="text", text="A"),
                SimpleNamespace(type="text", text="B"),
            ],
            usage=SimpleNamespace(
                input_tokens=4,
                cache_creation_input_tokens=2,
                cache_read_input_tokens=1,
                output_tokens=3,
            ),
        )
        create = MagicMock(return_value=response)
        client._client = SimpleNamespace(messages=SimpleNamespace(create=create))

        self.assertEqual(
            client.complete([{"role": "user", "content": "x"}], tier="fast"),
            "AB",
        )
        self.assertEqual(create.call_args.kwargs["max_tokens"], 8192)
        totals = client.usage_summary()["totals"]
        self.assertEqual(totals["prompt_tokens"], 7)
        self.assertEqual(totals["completion_tokens"], 3)
        self.assertEqual(totals["total_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
