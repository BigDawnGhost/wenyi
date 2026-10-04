"""Offline regressions for Responses parameters, credential aliases and endpoint resolution."""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config
from wenyi_core.envfile import load_env_file, read_env_values
from wenyi_core.llm import selection
from wenyi_core.llm.configuration import LLMConfig, ProviderConfig
from wenyi_core.llm.profiles import get_profile
from wenyi_core.llm.providers._credentials import candidate_env_vars, read_env_value
from wenyi_core.llm.providers._options import ProviderOptions
from wenyi_core.llm.providers.gemini import get_api_key_from_env
from wenyi_core.llm.registry import provider_spec
from wenyi_core.llm.routing import resolve_routes
from wenyi_core.llm.transport import RequestContext, ResolvedModel


@pytest.mark.parametrize(
    "kind,model", [("xai", "grok-4.6"), ("meta-ai", "muse-spark-1.2"), ("ramp", "gpt-5.4")]
)
def test_responses_requests_use_nested_reasoning(kind, model):
    spec = provider_spec(kind)
    client = spec.adapter_type()(ProviderConfig(kind=kind))
    context = RequestContext(
        "translation.body", "strong", 8192, lambda *a: None, lambda *a: None, lambda *a: None
    )
    body = client.build_body(
        [{"role": "user", "content": "hello"}],
        ResolvedModel(model, ProviderOptions(reasoning_effort="medium")),
        json_mode=False,
        context=context,
    )
    assert body["reasoning"] == {"effort": "medium"}
    assert "reasoning_effort" not in body


@pytest.mark.parametrize("kind", ["huggingface", "alibaba", "azure-foundry"])
def test_non_reasoning_profiles_accept_small_output_caps(kind):
    spec = provider_spec(kind)
    assert get_profile(kind).reasoning is None
    assert spec.output_limit(ProviderOptions(), None, 1024) == 1024
    assert spec.output_limit(ProviderOptions(thinking=True), None, 1024) == 1024
    assert spec.output_limit(ProviderOptions(), 600, None) == 600
    config = LLMConfig.model_validate(
        {
            "providers": {"p": {"kind": kind, "base_url": "https://example.invalid/v1"}},
            "models": {"m": {"provider": "p", "model": "mock-model", "max_output_tokens": 1024}},
            "tiers": {tier: "m" for tier in ("strong", "cheap", "fast")},
        }
    )
    assert resolve_routes(config)["translation.body"].max_output_tokens == 1024


def test_anthropic_native_thinking_still_reserves_output_budget():
    with pytest.raises(ValueError, match="max_output_tokens"):
        provider_spec("anthropic").output_limit(ProviderOptions(), None, 1024)
    assert (
        provider_spec("anthropic").output_limit(ProviderOptions(thinking=False), None, 1024) == 1024
    )


def test_gemini_selection_and_inference_share_key_priority(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-primary")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-fallback")
    selected = read_env_value(candidate_env_vars(get_profile("gemini")))
    assert selected == get_api_key_from_env(None)
    assert selected == ("fake-primary", "GEMINI_API_KEY")


def test_gemini_key_replacement_survives_next_cli_load(monkeypatch, tmp_path):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "")
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  preset: fake\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY='fake-old'\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "model",
            "--provider",
            "gemini",
            "--model",
            "mock-model",
            "--offline",
            "--yes",
            "--api-key",
            "fake-new",
        ],
    )
    assert result.exit_code == 0, result.output
    assert get_api_key_from_env(None)[0] == "fake-new"
    assert read_env_values(env)["GEMINI_API_KEY"] == "fake-new"
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    load_env_file(env)
    assert get_api_key_from_env(None)[0] == "fake-new"
    from wenyi_core.llm.providers.gemini import GeminiClient

    sdk_arguments = []
    monkeypatch.setattr("google.genai.Client", lambda **kw: sdk_arguments.append(kw) or object())
    GeminiClient(ProviderConfig(kind="gemini"))._ensure_client()
    assert sdk_arguments[0]["api_key"] == "fake-new"


def test_custom_endpoint_env_reaches_validation_preview_and_adapter(monkeypatch):
    endpoint = "https://example.invalid/v1"
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", endpoint)
    cfg = ProviderConfig(kind="openai-compatible")
    spec = provider_spec("custom")
    assert selection.effective_base_url("custom") == endpoint
    spec.validate_connection(cfg)
    assert spec.endpoint(cfg) == endpoint
    adapter = spec.adapter_type()(cfg)
    assert adapter.base_url == endpoint
    sdk_arguments = []
    monkeypatch.setattr("openai.OpenAI", lambda **kw: sdk_arguments.append(kw) or object())
    adapter._ensure_client()
    assert sdk_arguments[0]["base_url"] == endpoint
    explicit = ProviderConfig(kind="openai-compatible", base_url="https://override.invalid/v1")
    assert spec.endpoint(explicit) == "https://override.invalid/v1"


@pytest.mark.parametrize("endpoint", ["", "file:///invalid-endpoint"])
def test_custom_endpoint_environment_is_validated(monkeypatch, endpoint):
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", endpoint)
    with pytest.raises(ValueError):
        provider_spec("custom").validate_connection(ProviderConfig(kind="openai-compatible"))


def test_custom_endpoint_env_allows_offline_model_selection(monkeypatch, tmp_path):
    endpoint = "https://example.invalid/v1"
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", endpoint)
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  preset: fake\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "model",
            "--provider",
            "custom",
            "--model",
            "mock-model",
            "--offline",
            "--yes",
            "--no-credential",
        ],
    )
    assert result.exit_code == 0, result.output
    route = resolve_routes(Config.load(str(config)).llm)["translation.body"]
    assert route.endpoint == endpoint
    assert "OPENAI_COMPATIBLE_BASE_URL" in os.environ
