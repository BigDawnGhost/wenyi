"""Follow-up contracts pinned to the current Hermes provider runtime."""

import json
import time

import httpx
import pytest
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.oauth.credentials import OAuthCredential, OAuthCredentialError
from wenyi_core.llm.oauth.importers import parse_credential_text
from wenyi_core.llm.profiles import get_profile
from wenyi_core.llm.providers import _credentials
from wenyi_core.llm.providers.copilot import CopilotClient, model_wire


@pytest.mark.parametrize(
    "model,wire",
    [
        ("claude-sonnet-4-6", "chat"),
        ("gpt-5-mini", "chat"),
        ("gpt-5.4-mini", "responses"),
        ("gpt-6", "responses"),
        ("gpt-5.4-codex", "responses"),
        ("gemini-3.6-flash", "chat"),
    ],
)
def test_copilot_current_runtime_protocol(model, wire):
    assert model_wire(model) == wire


@pytest.mark.parametrize(
    "token,endpoints,expected",
    [
        ("test-only", {"api": "https://enterprise.invalid/v1"}, "https://enterprise.invalid/v1"),
        ("test-only;proxy-ep=proxy.enterprise.invalid", {}, "https://api.enterprise.invalid"),
        (
            "test-only;proxy-ep=proxy.other.invalid",
            {"api": "https://enterprise.invalid/v1"},
            "https://enterprise.invalid/v1",
        ),
    ],
)
def test_copilot_exchange_preserves_enterprise_endpoint(monkeypatch, token, endpoints, expected):
    response = httpx.Response(
        200,
        json={
            "token": token,
            "expires_at": time.time() + 3600,
            "endpoints": endpoints,
        },
    )

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return response

    monkeypatch.setattr(_credentials.httpx, "Client", Client)
    credential = _credentials.exchange_copilot_token(OAuthCredential(access_token="github-test"))
    assert credential.api_base_url == expected
    restored = parse_credential_text(json.dumps(credential.to_payload()))[0]
    assert restored.api_base_url == credential.api_base_url
    monkeypatch.setenv("WENYI_COPILOT_OAUTH", json.dumps(restored.to_payload()))
    client = CopilotClient(ProviderConfig(kind="copilot"))
    assert client.resolve_base_url() == expected
    override = CopilotClient(ProviderConfig(kind="copilot", base_url="https://explicit.invalid"))
    assert override.resolve_base_url() == "https://explicit.invalid"


def test_copilot_rejects_unsafe_credential_endpoint(monkeypatch):
    monkeypatch.setenv(
        "WENYI_COPILOT_OAUTH",
        json.dumps(
            {
                "access_token": "test-only",
                "plan": "copilot",
                "expires_at": time.time() + 3600,
                "api_base_url": "http://unsafe.invalid",
            }
        ),
    )
    with pytest.raises(OAuthCredentialError, match="HTTPS"):
        CopilotClient(ProviderConfig(kind="copilot")).resolve_base_url()


def test_vertex_presets_include_publisher_prefix():
    assert all(
        model.startswith("google/") for model in get_profile("vertex").preset_models().values()
    )


@pytest.mark.parametrize(
    "region,host",
    [
        ("global", "aiplatform.googleapis.com"),
        ("us-central1", "us-central1-aiplatform.googleapis.com"),
    ],
)
def test_vertex_endpoint_and_credential_path(monkeypatch, region, host):
    from types import SimpleNamespace

    import google.auth
    import google.auth.transport.requests

    credentials = SimpleNamespace(token="test-only", refresh=lambda request: None)
    calls = []
    monkeypatch.setattr(
        google.auth,
        "load_credentials_from_file",
        lambda path, scopes: (calls.append(path) or credentials, "embedded-project"),
    )
    monkeypatch.setattr(google.auth.transport.requests, "Request", lambda: None)
    monkeypatch.setenv("VERTEX_CREDENTIALS_PATH", "/not-read/test-only.json")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "explicit-project")
    monkeypatch.setenv("VERTEX_REGION", region)
    monkeypatch.delenv("VERTEX_BASE_URL", raising=False)
    profile = get_profile("vertex")
    token, endpoint = _credentials.vertex_token_and_base_url(profile, None)
    assert token == "test-only"
    assert endpoint == (
        f"https://{host}/v1beta1/projects/explicit-project/locations/{region}/endpoints/openapi"
    )
    assert calls == ["/not-read/test-only.json"]
    monkeypatch.setenv("VERTEX_BASE_URL", "https://override.invalid/v1")
    assert _credentials.vertex_token_and_base_url(profile, None)[1] == "https://override.invalid/v1"
