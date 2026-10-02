"""Local provider and model data behind the interactive ``wenyi model`` picker."""

from __future__ import annotations

import pytest
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.routing import resolve_routes
from wenyi_core.llm.selection import (
    candidate_models,
    effective_base_url,
    list_models,
    llm_section,
    preset_models,
    provider_choices,
)


def _choices() -> dict:
    return {choice.kind: choice for choice in provider_choices()}


def test_provider_choices_report_environment_credentials(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("WENYI_CODEX_OAUTH", '{"access_token": "token"}')

    choices = _choices()

    assert choices["deepseek"].configured is False
    assert choices["deepseek"].credential_envs == ("DEEPSEEK_API_KEY",)
    assert choices["deepseek"].preset_models["strong"] == "deepseek-flash"
    assert "deepseek-flash" in choices["deepseek"].known_models
    # An API-key provider writes a pasted key to its own variable and has no sign-in.
    assert choices["deepseek"].api_key_envs == ("DEEPSEEK_API_KEY",)
    assert choices["deepseek"].oauth_env is None
    assert choices["deepseek"].sign_in is None
    assert choices["deepseek"].base_url == "https://api.deepseek.com/v1"
    assert choices["deepseek"].base_url_env is None

    codex = choices["openai-codex"]
    assert codex.configured is True
    assert codex.credential_env == "WENYI_CODEX_OAUTH"
    assert codex.sign_in == "codex"
    assert codex.oauth_env == "WENYI_CODEX_OAUTH"
    assert codex.requires_api_key is False
    # The Codex subscription declares no models, so only a live list can offer them.
    assert codex.preset_models == {}
    assert codex.known_models == ()

    # Providers without a stored credential never advertise one.
    assert choices["vertex"].configured is False
    assert choices["vertex"].requires_api_key is False
    assert choices["vertex"].api_key_envs == ()


def test_provider_choices_cover_every_registered_kind_in_registry_order():
    from wenyi_core.llm.registry import PROVIDERS

    assert [choice.kind for choice in provider_choices()] == list(PROVIDERS)


def test_preset_models_come_from_the_profile_declaration():
    assert preset_models("anthropic") == {
        "strong": "claude-sonnet-4-6",
        "cheap": "claude-haiku-4-5-20251001",
        "fast": "claude-haiku-4-5-20251001",
    }
    assert preset_models("openai-codex") == {}


def test_llm_section_names_the_preset_and_keeps_tier_overrides():
    assert llm_section("deepseek") == {"preset": "deepseek"}

    overridden = llm_section("deepseek", {"strong": "deepseek-v4-pro"})
    assert overridden["preset"] == "deepseek"
    assert overridden["models"]["default_strong"] == {
        "provider": "default",
        "model": "deepseek-v4-pro",
        # The reasoning options declared for this tier survive the model replacement.
        "options": {"thinking": True, "reasoning_effort": "high", "extra_body": {}},
    }
    assert "default_cheap" not in overridden["models"]


def test_llm_section_writes_a_connection_override_without_losing_the_kind():
    section = llm_section("deepseek", base_url="https://proxy.example/v1")

    # The preset supplies ``kind``; the override must keep it when the maps merge.
    assert section == {
        "preset": "deepseek",
        "providers": {"default": {"kind": "deepseek", "base_url": "https://proxy.example/v1"}},
    }
    resolved = LLMConfig.model_validate(section)
    assert resolved.providers["default"].base_url == "https://proxy.example/v1"
    assert resolve_routes(resolved)["translation.body"].model == "deepseek-flash"


def test_effective_base_url_prefers_the_environment_override(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPATIBLE_BASE_URL", raising=False)

    assert effective_base_url("deepseek") == "https://api.deepseek.com/v1"
    assert effective_base_url("openai-compatible") is None

    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://127.0.0.1:11434/v1")
    assert effective_base_url("openai-compatible") == "http://127.0.0.1:11434/v1"


def test_candidate_models_only_offers_the_live_catalog(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    _stub_http(monkeypatch, {"data": [{"id": "vendor/new"}, {"id": "vendor/model-a"}]})

    candidates, failure = candidate_models("openrouter")

    assert failure == ""
    assert candidates == ("vendor/new", "vendor/model-a")


def test_candidate_models_reports_failure_without_declared_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    _stub_http(monkeypatch, {}, status_code=500)

    candidates, failure = candidate_models("openrouter")

    assert "HTTP 500" in failure
    assert candidates == ()


def test_candidate_models_skips_the_network_when_asked(monkeypatch):
    monkeypatch.setattr(
        "wenyi_core.llm.selection.httpx.Client",
        lambda **kw: pytest.fail("--offline must not reach the provider"),
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert candidate_models("deepseek", offline=True) == (("deepseek-flash", "deepseek-v4-pro"), "")


def test_llm_section_expands_providers_without_a_preset():
    with pytest.raises(ValueError, match="declares no built-in models"):
        llm_section("openai-codex")

    section = llm_section(
        "openai-codex",
        {"strong": "gpt-5-codex", "cheap": "gpt-5-mini", "fast": "gpt-5-mini"},
    )
    assert section["providers"] == {"default": {"kind": "openai-codex"}}
    assert section["tiers"] == {
        "strong": "default_strong",
        "cheap": "default_cheap",
        "fast": "default_fast",
    }
    routes = resolve_routes(LLMConfig.model_validate(section))
    assert routes["translation.body"].model == "gpt-5-codex"
    assert routes["review.scan"].model == "gpt-5-mini"


def test_list_models_reports_providers_that_publish_no_list(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        list_models("deepseek")


def test_list_models_uses_the_subscription_client(monkeypatch):
    from wenyi_core.llm.providers.codex import CodexClient

    monkeypatch.setenv("WENYI_CODEX_OAUTH", '{"access_token": "token"}')
    monkeypatch.setattr(
        CodexClient, "available_models", lambda self, credential: ("gpt-5-codex", "gpt-5-mini")
    )
    assert list_models("openai-codex") == ("gpt-5-codex", "gpt-5-mini")


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _StubClient:
    """Stand-in for ``httpx.Client`` that records the request and returns one payload."""

    calls: list = []

    def __init__(self, payload, status_code=200):
        self._response = _StubResponse(payload, status_code)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, headers=None):
        _StubClient.calls.append((url, dict(headers or {})))
        return self._response


def _stub_http(monkeypatch, payload, status_code=200):
    _StubClient.calls = []
    monkeypatch.setattr(
        "wenyi_core.llm.selection.httpx.Client", lambda **kw: _StubClient(payload, status_code)
    )


def test_list_models_reads_a_profile_model_endpoint(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    _stub_http(monkeypatch, {"data": [{"id": "vendor/model-a"}, {"id": "vendor/model-b"}]})

    assert list_models("openrouter") == ("vendor/model-a", "vendor/model-b")
    url, headers = _StubClient.calls[0]
    assert url == "https://openrouter.ai/api/v1/models"
    assert headers["authorization"] == "Bearer secret"


def test_live_catalog_uses_the_selected_endpoint(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-token")
    _stub_http(monkeypatch, {"data": [{"id": "live-only"}]})
    assert candidate_models("deepseek", base_url="https://proxy.example/v1") == (("live-only",), "")
    assert _StubClient.calls == [
        ("https://proxy.example/v1/models", {"authorization": "Bearer test-token"})
    ]


def test_gemini_catalog_filters_non_generation_models(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-token")
    _stub_http(
        monkeypatch,
        {
            "models": [
                {"name": "models/live-gemini", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
            ]
        },
    )
    assert list_models("gemini") == ("live-gemini",)
    assert _StubClient.calls[0][0].endswith("/v1beta/models")
    assert _StubClient.calls[0][1]["x-goog-api-key"] == "test-token"


def test_antigravity_live_catalog_uses_authorized_project(monkeypatch):
    import httpx
    from wenyi_core.llm.providers.wire.gemini_cloudcode import CloudCodeClient

    monkeypatch.setenv(
        "WENYI_ANTIGRAVITY_OAUTH", '{"access_token":"test-token","project_id":"test-project"}'
    )
    calls = []

    def respond(request):
        import json

        calls.append(request)
        assert json.loads(request.content) == {"project": "test-project"}
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"models": {"live-gemini": {}, "live-claude": {}}})

    monkeypatch.setattr(
        CloudCodeClient,
        "http_client",
        lambda self: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    assert list_models("antigravity") == ("live-gemini", "live-claude")
    assert (
        str(calls[0].url) == "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
    )


def test_list_models_reports_an_unusable_model_endpoint(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    _stub_http(monkeypatch, {}, status_code=500)

    with pytest.raises(ValueError, match="HTTP 500"):
        list_models("openrouter")


def test_list_models_needs_the_credential_environment(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        list_models("openrouter")
