"""Thinking overrides change one operation's effective request, not the registry."""

from types import SimpleNamespace

import pytest
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.router import RoutedLLMClient
from wenyi_core.llm.routing import inference_snapshot, resolve_routes


def test_compatible_thinking_respects_connection_dialect():
    ref = "proxy/model"
    document = {
        "preset": "fake",
        "providers": {"proxy": {"kind": "openai-compatible", "base_url": "https://api.invalid"}},
        "models": {ref: {}},
        "routes": {"translation.body": {"model": ref, "thinking": "xhigh"}},
    }
    with pytest.raises(ValueError, match=r"translation\.body.*thinking"):
        LLMConfig.model_validate(document)
    document["providers"]["proxy"]["reasoning_style"] = "openai"
    route = resolve_routes(LLMConfig.model_validate(document))["translation.body"]
    assert route.request_model().options.reasoning_effort == "xhigh"
    assert route.request_model().options.thinking is True


def test_shared_model_can_use_distinct_effort_with_or_without_output_hint():
    ref = "deepseek/deepseek-flash"
    base = LLMConfig.model_validate({"preset": "deepseek"})
    config = LLMConfig.model_validate(
        {
            "preset": "deepseek",
            "routes": {
                "translation.body": {"model": ref, "thinking": "max"},
                "review.scan": {"model": ref, "thinking": "off"},
            },
        }
    )
    assert config.models == base.models
    assert inference_snapshot(base, ("analysis.style",)) == inference_snapshot(
        config, ("analysis.style",)
    )
    assert inference_snapshot(base, ("translation.body",)) != inference_snapshot(
        config, ("translation.body",)
    )

    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    client = RoutedLLMClient(config)
    client.adapter("deepseek")._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=complete))
    )
    client.complete([], operation="translation.body", max_tokens=256)
    client.complete([], operation="review.scan", max_tokens=256)
    assert calls[0]["reasoning_effort"] == "max"
    assert calls[0]["max_tokens"] == 4096
    assert calls[1]["extra_body"]["thinking"]["type"] == "disabled"
    assert "reasoning_effort" not in calls[1]
    assert calls[1]["max_tokens"] == 256


def test_gemini_override_replaces_the_budget_only_for_that_operation():
    ref = "gemini/gemini-3.6-flash"
    config = LLMConfig.model_validate(
        {
            "preset": "gemini",
            "models": {ref: {"options": {"thinking_budget": 4000}}},
            "routes": {"translation.body": {"model": ref, "thinking": "low"}},
        }
    )
    routes = resolve_routes(config)
    assert routes["translation.body"].request_model().options.thinking_level == "low"
    assert routes["translation.body"].request_model().options.thinking_budget is None
    assert routes["translation.title"].request_model().options.thinking_budget == 4000


def test_fallback_uses_its_own_model_settings():
    config = LLMConfig.model_validate(
        {
            "preset": "deepseek",
            "providers": {"local": {"kind": "fake"}},
            "models": {"local/fake": {}},
            "routes": {
                "translation.body": {
                    "model": "deepseek/deepseek-flash",
                    "thinking": "max",
                    "fallbacks": ["local/fake"],
                }
            },
        }
    )
    snapshot = RoutedLLMClient(config).request_snapshot("translation.body")
    assert snapshot["primary"]["options"]["reasoning_effort"] == "max"
    assert snapshot["fallbacks"][0]["options"] == {}


@pytest.mark.parametrize(
    ("preset", "model", "options", "level"),
    [
        ("deepseek", "deepseek/deepseek-flash", {}, "medium"),
        ("fake", "fake/fake", {}, "high"),
        ("gemini", "gemini/gemini-2.5-flash", {}, "high"),
        (
            "deepseek",
            "deepseek/deepseek-flash",
            {"extra_body": {"thinking": {"type": "disabled"}}},
            "high",
        ),
    ],
)
def test_inapplicable_overrides_are_rejected_at_the_route(preset, model, options, level):
    with pytest.raises(ValueError, match=r"translation\.body.*thinking"):
        LLMConfig.model_validate(
            {
                "preset": preset,
                "models": {model: {"options": options}},
                "routes": {"translation.body": {"model": model, "thinking": level}},
            }
        )
