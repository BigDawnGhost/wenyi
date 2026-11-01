"""The shared application exposes codes without changing any existing detail text."""

from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_backend.errors import PROJECT_NOT_FOUND


def _client(backend_context, monkeypatch):
    backend_context.repository.get_project.return_value = None
    return TestClient(create_app(backend_context))


def test_real_route_returns_a_code_and_the_original_detail(backend_context, monkeypatch):
    response = _client(backend_context, monkeypatch).get("/projects/missing-project")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == PROJECT_NOT_FOUND
    assert body["detail"] == "project not found"
    assert response.headers["x-request-id"]


def test_envelope_request_id_matches_the_response_header(backend_context, monkeypatch):
    client = _client(backend_context, monkeypatch)
    response = client.get("/projects/missing-project", headers={"x-request-id": "trace-1"})
    assert response.json()["request_id"] == "trace-1"
    assert response.headers["x-request-id"] == "trace-1"
