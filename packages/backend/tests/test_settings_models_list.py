"""Draft model listing uses the typed key, the platform credential port, or the environment."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_backend.context import use_context
from wenyi_backend.global_settings import GlobalSettings
from wenyi_core.config import Config
from wenyi_core.llm.models import ModelListing

SECRET = "fake-typed-list-key"


@pytest.fixture
def api(backend_context, monkeypatch):
    saved = Config.from_dict(
        {
            "llm": {
                "preset": "deepseek",
                "providers": {"default": {"kind": "deepseek", "api_key_env": "SAVED_KEY"}},
            }
        }
    )
    calls = []

    def fake_list(provider, *, credential=None, use_credential=False, timeout=20):
        calls.append(
            {
                "kind": provider.kind,
                "credential": credential,
                "injected": use_credential,
                "timeout": timeout,
            }
        )
        return ModelListing(True, ("m-a", "m-b"))

    monkeypatch.setattr(
        backend_context.settings_store, "load", lambda **kw: GlobalSettings(saved), raising=False
    )
    import wenyi_backend.routers.settings as module

    monkeypatch.setattr(module, "list_connection_models", fake_list)

    def client_for(context):
        return TestClient(create_app(context))

    return client_for, calls


def test_typed_key_is_injected_and_never_echoed(api, backend_context):
    client_for, calls = api
    with client_for(backend_context) as client:
        response = client.post(
            "/settings/models/list",
            json={"connection": "draft", "provider": {"kind": "openai"}, "api_key": SECRET},
        )
    assert response.status_code == 200
    assert response.json()["models"] == ["m-a", "m-b"]
    assert calls == [{"kind": "openai", "credential": SECRET, "injected": True, "timeout": 20}]
    assert SECRET not in response.text


def test_web_reads_environment_only_for_known_variable_names(api, backend_context):
    client_for, calls = api
    with client_for(backend_context) as client:
        ok = client.post("/settings/models/list", json={"provider": {"kind": "deepseek"}})
        saved = client.post(
            "/settings/models/list",
            json={"provider": {"kind": "openai", "api_key_env": "SAVED_KEY"}},
        )
        other = client.post(
            "/settings/models/list",
            json={"provider": {"kind": "openai", "api_key_env": "HOME"}},
        )
    assert ok.status_code == saved.status_code == 200
    assert [call["injected"] for call in calls] == [False, False]
    assert other.status_code == 422
    assert "HOME" in other.json()["detail"]


def test_platform_port_supplies_the_stored_key(api, backend_context):
    client_for, calls = api
    seen = []

    def resolve(connection, provider):
        seen.append((connection, provider.kind))
        return "fake-stored-key"

    context = replace(backend_context, connection_credential=resolve)
    with use_context(context), client_for(context) as client:
        response = client.post(
            "/settings/models/list",
            json={"connection": "default", "provider": {"kind": "deepseek"}},
        )
    assert response.status_code == 200
    assert seen == [("default", "deepseek")]
    assert calls[-1]["credential"] == "fake-stored-key"


@pytest.mark.parametrize(
    "body",
    [
        {"provider": {"kind": "unknown"}},
        {"provider": {"kind": "openai", "base_url": "ftp://x"}},
        {"provider": {"kind": "openai"}, "timeout": 0},
        {"provider": {"kind": "openai"}, "extra": True, "api_key": SECRET},
    ],
)
def test_invalid_requests_are_rejected_without_echo(api, backend_context, body):
    client_for, calls = api
    with client_for(backend_context) as client:
        response = client.post("/settings/models/list", json=body)
    assert response.status_code == 422
    assert SECRET not in response.text
    assert calls == []


def test_listing_failures_are_returned_as_results(backend_context, monkeypatch):
    monkeypatch.setattr(
        backend_context.settings_store,
        "load",
        lambda **kw: GlobalSettings(Config()),
        raising=False,
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    # The real implementation runs offline here: no credential means no request is sent.
    with TestClient(create_app(backend_context)) as client:
        response = client.post("/settings/models/list", json={"provider": {"kind": "deepseek"}})
    assert response.status_code == 200
    assert response.json()["category"] == "missing_credential"
