"""Offline regression contracts for Hermes-compatible provider behaviour."""

import json
import time
from dataclasses import replace

import pytest
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.oauth.credentials import OAuthCredential
from wenyi_core.llm.profiles import get_profile
from wenyi_core.llm.providers._options import ProviderOptions
from wenyi_core.llm.providers.wire.openai_chat import OpenAIChatClient
from wenyi_core.llm.registry import provider_spec


def test_provider_alias_is_routable():
    cfg = ProviderConfig(kind="claude")
    assert cfg.kind == "anthropic"
    assert provider_spec("claude") is provider_spec("anthropic")


def test_environment_endpoint_is_validated_and_previewed(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "https://foundry.invalid/v1")
    cfg = ProviderConfig(kind="azure-foundry")
    spec = provider_spec(cfg.kind)
    spec.validate_connection(cfg)
    assert spec.endpoint(cfg) == "https://foundry.invalid/v1"
    monkeypatch.setenv("AZURE_FOUNDRY_BASE_URL", "not-a-url")
    with pytest.raises(ValueError, match="absolute HTTP"):
        spec.validate_connection(cfg)


def test_refreshed_oauth_token_is_reused_and_rotated(monkeypatch):
    from wenyi_core.llm.providers import _credentials

    original = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    monkeypatch.setenv("WENYI_QWEN_OAUTH", json.dumps(original.to_payload()))
    calls = []

    def refresh(profile, credential, env_name):
        calls.append(credential.refresh_token)
        return replace(
            credential,
            access_token=f"new-{len(calls)}",
            refresh_token=f"refresh-{len(calls)}",
            expires_at=time.time() + 3600,
        )

    monkeypatch.setattr(_credentials, "_refresh", refresh)
    client = OpenAIChatClient(ProviderConfig(kind="qwen-oauth"))
    assert client.auth_token() == "new-1"
    assert client.auth_token() == "new-1"
    assert calls == ["refresh-old"]
    monkeypatch.setattr(OAuthCredential, "expired", lambda self: True)
    assert client.auth_token() == "new-2"
    assert calls == ["refresh-old", "refresh-1"]


def test_plain_api_key_is_not_an_expired_subscription(monkeypatch):
    monkeypatch.delenv("WENYI_XAI_OAUTH", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "api-key")
    client = OpenAIChatClient(ProviderConfig(kind="xai"))
    assert client.auth_token() == "api-key"


def test_kimi_code_key_uses_coding_endpoint(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-test-only")
    spec = provider_spec("kimi-coding")
    cfg = ProviderConfig(kind="kimi-coding")
    client = spec.adapter_type()(cfg)
    assert client.resolve_base_url() == "https://api.kimi.com/coding"
    assert spec.endpoint(cfg) == "https://api.kimi.com/coding"


def test_anthropic_usage_includes_cache_tokens():
    from wenyi_core.llm.providers.wire.anthropic import anthropic_usage

    usage = anthropic_usage(
        {
            "usage": {
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 30,
            }
        }
    )
    assert usage.prompt_tokens == 60
    assert usage.total_tokens == 63
    assert usage.cache_hit_tokens == 20
    assert usage.cache_miss_tokens == 40


def test_minimax_profile_sends_protocol_version(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-only")
    client = provider_spec("minimax").adapter_type()(ProviderConfig(kind="minimax"))
    assert client.request_headers()["anthropic-version"] == "2023-06-01"


def test_zai_plan_endpoints_are_explicit():
    assert get_profile("zai-coding-plan").base_url == "https://api.z.ai/api/coding/paas/v4"
    assert get_profile("zai-cn").base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert get_profile("zai-coding-plan-cn").base_url == (
        "https://open.bigmodel.cn/api/coding/paas/v4"
    )


@pytest.mark.parametrize("kind", ["kimi-coding", "copilot", "zai-coding-plan"])
def test_dedicated_profiles_keep_presets(kind):
    from wenyi_core.llm.configuration import LLMConfig

    cfg = LLMConfig.model_validate({"preset": kind})
    assert cfg.providers["default"].kind == kind
    assert set(cfg.tiers) == {"strong", "cheap", "fast"}


def test_kimi_code_request_uses_messages(monkeypatch):
    from wenyi_core.llm.transport import RequestContext, ResolvedModel

    monkeypatch.setenv("KIMI_API_KEY", "sk-kimi-test-only")
    client = provider_spec("kimi-coding").adapter_type()(ProviderConfig(kind="kimi-coding"))
    bodies = []
    monkeypatch.setattr(
        client,
        "post_json",
        lambda path, body: (
            bodies.append((path, body))
            or {
                "content": [{"type": "text", "text": "ok"}],
            }
        ),
    )
    context = RequestContext(
        "translate", "strong", 8192, lambda **kw: None, lambda usage: None, lambda: None
    )
    assert (
        client._request(
            [{"role": "user", "content": "hi"}],
            ResolvedModel("k3", ProviderOptions(temperature=0.5)),
            json_mode=True,
            context=context,
        )
        == "ok"
    )
    path, body = bodies[0]
    assert path == "/v1/messages"
    assert "temperature" not in body
    assert "reasoning_effort" not in body
    assert "json" in body["system"]


def test_copilot_model_protocols_and_headers(monkeypatch):
    from wenyi_core.llm.providers.copilot import model_wire
    from wenyi_core.llm.providers.wire.responses import ResponsesOutcome
    from wenyi_core.llm.transport import RequestContext, ResolvedModel

    monkeypatch.setenv(
        "WENYI_COPILOT_OAUTH",
        json.dumps(
            OAuthCredential(
                access_token="test-only",
                plan="copilot",
                expires_at=time.time() + 3600,
            ).to_payload()
        ),
    )
    client = provider_spec("copilot").adapter_type()(ProviderConfig(kind="copilot"))
    assert client.request_headers()["copilot-integration-id"] == "vscode-chat"
    assert client.request_headers()["x-initiator"] == "user"
    bodies = []
    monkeypatch.setattr(
        client, "read_outcome", lambda body: bodies.append(body) or ResponsesOutcome(text="ok")
    )
    context = RequestContext(
        "translate", "strong", 8192, lambda **kw: None, lambda usage: None, lambda: None
    )
    assert (
        client._request(
            [{"role": "user", "content": "hi"}],
            ResolvedModel("gpt-5.4", ProviderOptions()),
            json_mode=False,
            context=context,
        )
        == "ok"
    )
    assert bodies[0]["reasoning"] == {"effort": "medium"}
    assert model_wire("claude-sonnet-4-6") == "chat"
    assert model_wire("gpt-4.1") == "chat"


def test_environment_rotation_invalidates_credential_cache(monkeypatch):
    monkeypatch.setenv("DEEPINFRA_API_KEY", "first-test-only")
    client = OpenAIChatClient(ProviderConfig(kind="deepinfra"))
    assert client.auth_token() == "first-test-only"
    monkeypatch.setenv("DEEPINFRA_API_KEY", "second-test-only")
    assert client.auth_token() == "second-test-only"


def test_optional_api_key_profile_works_without_credentials():
    client = OpenAIChatClient(
        ProviderConfig(
            kind="openai-compatible",
            base_url="http://localhost:11434/v1",
        )
    )
    assert client.auth_token() == "no-key"


def test_vertex_token_cache_is_not_permanent(monkeypatch):
    from wenyi_core.llm.providers.wire import base

    minted = []
    clock = [1000.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        base,
        "vertex_token_and_base_url",
        lambda profile, url: (
            minted.append("mint") or f"token-{len(minted)}",
            "https://vertex.invalid/v1",
        ),
    )
    client = OpenAIChatClient(ProviderConfig(kind="vertex"))
    assert client.auth_token() == "token-1"
    assert client.auth_token() == "token-1"
    clock[0] += 301
    assert client.auth_token() == "token-2"


def test_responses_stream_without_terminal_event_is_truncated(monkeypatch):
    from contextlib import contextmanager

    import httpx
    from wenyi_core.llm.providers.wire.responses import ResponsesClient
    from wenyi_core.llm.retrying import TruncatedResponseError

    client = ResponsesClient(ProviderConfig(kind="xai"))

    @contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=('data: {"type":"response.output_text.delta","delta":"partial"}\n\n'),
        )

    monkeypatch.setattr(client, "open_stream", stream)
    outcome = client.read_outcome({})
    with pytest.raises(TruncatedResponseError):
        outcome.answer("xAI")


def test_copilot_millisecond_expiry_is_normalized():
    from wenyi_core.llm.providers._credentials import _copilot_expiry

    assert _copilot_expiry({"expires_at": 2_000_000_000_000}) == 2_000_000_000


def test_kimi_legacy_key_keeps_chat_protocol(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "legacy-test-only")
    client = provider_spec("kimi-coding").adapter_type()(ProviderConfig(kind="kimi-coding"))
    bodies = []
    monkeypatch.setattr(
        client,
        "post_json",
        lambda path, body: (
            bodies.append((path, body))
            or {
                "choices": [{"message": {"content": "ok"}}],
            }
        ),
    )
    from wenyi_core.llm.transport import RequestContext, ResolvedModel

    context = RequestContext(
        "translate", "strong", 8192, lambda **kw: None, lambda usage: None, lambda: None
    )
    assert (
        client._request(
            [{"role": "user", "content": "hi"}],
            ResolvedModel("kimi-k2.6", ProviderOptions()),
            json_mode=False,
            context=context,
        )
        == "ok"
    )
    assert bodies[0][0] == "/chat/completions"
