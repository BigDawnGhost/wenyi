"""Saved and draft model probes use isolated settings and platform credentials."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_backend.global_settings import GlobalSettings
from wenyi_core.config import Config
from wenyi_core.llm.connectivity import ProbeResult


class ProbingClient:
    def __init__(self, config, results=(), error=None):
        self.config = config
        self.results = list(results)
        self.error = error
        self.calls = []

    def probe_profiles(self, profiles=None, *, timeout=None):
        self.calls.append((profiles, timeout))
        if self.error is not None:
            raise self.error
        return self.results


def _result(**overrides):
    values = dict(
        profile="deepseek/deepseek-flash",
        connection="deepseek",
        provider="deepseek",
        model="deepseek-flash",
        endpoint="https://api.deepseek.com",
        operations=("translation.body",),
        ok=True,
        category="ok",
        message="The provider replied.",
        latency_ms=120,
    )
    return ProbeResult(**{**values, **overrides})


@pytest.fixture
def api(backend_context, monkeypatch):
    saved = Config.from_dict({"llm": {"preset": "deepseek"}})
    state = SimpleNamespace(client=None, configs=[])

    def build(config):
        state.configs.append(config)
        return state.client

    monkeypatch.setattr(
        backend_context.settings_store, "load", lambda **kw: GlobalSettings(saved), raising=False
    )
    object.__setattr__(backend_context, "build_client", build)
    client = TestClient(create_app(backend_context))
    try:
        yield client, state, saved
    finally:
        client.close()


def test_ping_probes_saved_settings_without_a_project(api):
    client, state, saved = api
    state.client = ProbingClient(
        saved,
        [
            _result(),
            _result(
                profile="deepseek/spare",
                model="spare",
                ok=False,
                category="authentication_failed",
                status_code=401,
            ),
        ],
    )
    response = client.post("/settings/models/ping", json={"timeout": 5})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["profile"] for item in body] == ["deepseek/deepseek-flash", "deepseek/spare"]
    assert body[0]["operations"] == ["translation.body"]
    assert body[1] | {"ok": False, "status_code": 401} == body[1]
    assert state.configs == [saved]
    assert state.client.calls == [(None, 5)]


def test_ping_only_probes_selected_connection(api):
    client, state, saved = api
    from wenyi_core.llm.configuration import ModelConfig, ProviderConfig

    saved.llm.providers["other"] = ProviderConfig(kind="fake")
    saved.llm.models["other/model"] = ModelConfig()
    state.client = ProbingClient(saved, [_result()])
    response = client.post("/settings/models/ping", json={"connection": "deepseek"})
    assert response.status_code == 200, response.text
    assert state.client.calls == [(["deepseek/deepseek-flash"], 30)]
    assert client.post("/settings/models/ping", json={"connection": "missing"}).status_code == 422


def test_ping_defaults_and_bounds_the_timeout(api):
    client, state, saved = api
    state.client = ProbingClient(saved)
    assert client.post("/settings/models/ping", json={}).status_code == 200
    assert state.client.calls == [(None, 30)]
    for timeout in (0, 121):
        assert client.post("/settings/models/ping", json={"timeout": timeout}).status_code == 422
    assert client.post("/settings/models/ping", json={"workflow": "x"}).status_code == 422


def test_ping_configuration_errors_are_actionable(api):
    client, state, saved = api
    state.client = ProbingClient(saved, error=RuntimeError("Unsupported client"))
    response = client.post("/settings/models/ping", json={})
    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported client"


def test_ping_route_is_in_the_shared_schema(backend_context):
    schema = create_app(backend_context).openapi()
    assert "post" in schema["paths"]["/settings/models/ping"]
    assert "ModelPingResult" in schema["components"]["schemas"]


@pytest.fixture
def draft_probe(monkeypatch):
    calls = []

    def probe(connection, provider, models, *, credentials, timeout):
        calls.append((connection, provider, models, credentials, timeout))
        return [_result(connection=connection, model="new-model")]

    monkeypatch.setattr("wenyi_backend.routers.settings.probe_draft_connection", probe)
    return calls


def test_web_tests_the_draft_without_persisting_it(api, draft_probe):
    client, state, saved = api
    state.client = ProbingClient(saved)
    body = {
        "draft_id": "draft",
        "provider": {"kind": "deepseek", "base_url": "https://draft.invalid"},
        "models": {"new-model": {"max_output_tokens": 200}},
    }
    response = client.post("/settings/models/ping/draft", json=body)
    assert response.status_code == 200, response.text
    connection, provider, models, credentials, timeout = draft_probe.pop()
    assert connection == "draft" and provider.base_url == "https://draft.invalid"
    assert models["new-model"].max_output_tokens == 200
    assert credentials is None and timeout == 30
    assert client.get("/settings").json()["effective"]["llm"]["providers"].keys() == {"deepseek"}
    assert state.configs == []
    for extra in (
        {"api_key": "fake-probe-key"},
        {"provider": {"kind": "deepseek", "api_key_env": "HOME"}},
    ):
        rejected = client.post("/settings/models/ping/draft", json=body | extra)
        assert rejected.status_code == 422
        assert "fake-probe-key" not in rejected.text
    assert not draft_probe


def test_desktop_draft_takes_typed_or_stored_key_only_in_memory(backend_context, draft_probe):
    stored = []

    def resolve(connection, provider):
        stored.append((connection, provider.kind))
        return "fake-stored-key"

    context = replace(backend_context, connection_credential=resolve)
    body = {
        "connection": "deepseek",
        "draft_id": "renamed",
        "provider": {"kind": "deepseek"},
        "models": {"new-model": {}},
    }
    with TestClient(create_app(context)) as client:
        for key in (None, " fake-new-key ", ""):
            response = client.post("/settings/models/ping/draft", json=body | {"api_key": key})
            assert response.status_code == 200, response.text
            assert "fake-" not in response.text
        invalid = client.post(
            "/settings/models/ping/draft", json=body | {"api_key": ["fake-unparseable-key"]}
        )
        assert invalid.status_code == 422
        assert "fake-unparseable-key" not in invalid.text
    assert stored == [("deepseek", "deepseek"), (None, "deepseek")]
    assert [call[3] for call in draft_probe] == [
        {"renamed": "fake-stored-key"},
        {"renamed": "fake-new-key"},
        {"renamed": "fake-stored-key"},
    ]
