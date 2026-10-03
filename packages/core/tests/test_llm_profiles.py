"""Offline contracts for the declarative provider profiles and their registration.

A profile is data: it must become a routable provider, use the adapter that speaks its wire,
and contribute its own preset, endpoint and output cap without any per-vendor module.
"""

import pytest
from wenyi_core.llm.configuration import LLMConfig, ProviderConfig
from wenyi_core.llm.profiles import PROFILES, get_profile
from wenyi_core.llm.providers._options import ProviderOptions
from wenyi_core.llm.registry import PROVIDERS, provider_spec
from wenyi_core.llm.routing import resolve_routes

PROFILE_KINDS = [profile.kind for profile in PROFILES]


def test_every_profile_is_routable():
    assert set(PROFILE_KINDS) <= set(PROVIDERS)


@pytest.mark.parametrize("kind", PROFILE_KINDS)
def test_profile_connection_validates_against_its_own_endpoint(kind):
    profile = get_profile(kind)
    provider_spec(kind).validate_connection(
        ProviderConfig(kind=kind, base_url=profile.base_url or "https://example.invalid/v1")
    )


@pytest.mark.parametrize("kind", PROFILE_KINDS)
def test_profile_rejects_unknown_connection_options(kind):
    with pytest.raises(ValueError):
        provider_spec(kind).validate_connection(
            ProviderConfig(kind=kind, base_url="https://example.invalid/v1", timeout_seconds=1)
        )


def test_profile_provider_uses_the_adapter_for_its_wire():
    from wenyi_core.llm.providers.wire.anthropic import AnthropicClient
    from wenyi_core.llm.providers.wire.gemini_cloudcode import CloudCodeClient
    from wenyi_core.llm.providers.wire.openai_chat import OpenAIChatClient
    from wenyi_core.llm.providers.wire.responses import ResponsesClient

    assert provider_spec("anthropic").adapter_type() is AnthropicClient
    assert provider_spec("zai").adapter_type() is OpenAIChatClient
    assert provider_spec("xai").adapter_type() is ResponsesClient
    assert provider_spec("antigravity").adapter_type() is CloudCodeClient


def test_profile_preset_uses_the_declared_tiers():
    preset = provider_spec("anthropic").preset()
    assert preset["providers"] == {"default": {"kind": "anthropic"}}
    assert preset["models"]["default_strong"]["model"] == "claude-sonnet-4-6"
    assert preset["tiers"] == {
        "strong": "default_strong",
        "cheap": "default_cheap",
        "fast": "default_fast",
    }


def test_profile_preset_expands_into_a_routable_configuration():
    config = LLMConfig.model_validate({"preset": "anthropic"})
    route = resolve_routes(config)["translation.body"]
    assert route.provider_kind == "anthropic"
    assert route.endpoint == "https://api.anthropic.com"
    assert route.model == "claude-sonnet-4-6"


def test_profile_without_models_has_no_preset():
    with pytest.raises(ValueError, match="no preset"):
        provider_spec("openai-codex").preset()


def test_profile_endpoint_prefers_the_connection_and_ignores_trailing_slash():
    provider = provider_spec("anthropic")
    assert provider.endpoint(ProviderConfig(kind="anthropic")) == "https://api.anthropic.com"
    override = ProviderConfig(kind="anthropic", base_url="https://relay.invalid/v1/")
    assert provider.endpoint(override) == "https://relay.invalid/v1/"


def test_profile_output_limit_uses_the_declared_default():
    assert provider_spec("antigravity").output_limit(ProviderOptions(), None, None) == 65536
    assert provider_spec("meta-ai").output_limit(ProviderOptions(), None, None) == 16384


def test_profile_output_limit_keeps_an_explicit_limit_over_the_default():
    assert provider_spec("antigravity").output_limit(ProviderOptions(), None, 8192) == 8192


def test_profile_output_limit_reserves_room_for_thinking():
    assert provider_spec("nous").output_limit(ProviderOptions(), 1000, None) == 4096


def test_profile_alias_is_a_connection_kind():
    assert get_profile("claude") is get_profile("anthropic")
    assert provider_spec("claude") is provider_spec("anthropic")
    cfg = LLMConfig.model_validate({"preset": "google"})
    assert cfg.providers["default"].kind == "gemini"
