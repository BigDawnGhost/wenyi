"""Global model connectivity tests use saved settings and the platform credential snapshot."""

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
        self.calls.append(timeout)
        if self.error is not None:
            raise self.error
        return self.results


def _result(**overrides):
    values = dict(
        profile="default_strong",
        connection="default",
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
            _result(profile="spare", ok=False, category="authentication_failed", status_code=401),
        ],
    )
    response = client.post("/settings/models/ping", json={"timeout": 5})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["profile"] for item in body] == ["default_strong", "spare"]
    assert body[0]["operations"] == ["translation.body"]
    assert body[1] | {"ok": False, "status_code": 401} == body[1]
    assert state.configs == [saved]
    assert state.client.calls == [5]


def test_ping_defaults_and_bounds_the_timeout(api):
    client, state, saved = api
    state.client = ProbingClient(saved)
    assert client.post("/settings/models/ping", json={}).status_code == 200
    assert state.client.calls == [30]
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
