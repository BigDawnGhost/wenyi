"""Migrated configuration raises carry codes without changing their text."""

import pytest
from fastapi import HTTPException
from wenyi_backend.errors import INVALID_CONFIGURATION, ApiError


def _client(backend_context, monkeypatch, project):
    from fastapi.testclient import TestClient
    from wenyi_backend import dal
    from wenyi_backend.application import create_app

    monkeypatch.setattr(dal, "get_project", lambda pid: project)
    return TestClient(create_app(backend_context))


def test_invalid_project_yaml_returns_invalid_configuration(backend_context, monkeypatch):
    project = {"id": "book", "fmt": "epub", "status": "prepared", "initialized": True}
    client = _client(backend_context, monkeypatch, project)
    response = client.post("/projects/book/config/validate", json={"yaml": "language: [unclosed"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == INVALID_CONFIGURATION
    assert body["detail"] == body["message"]


def test_configuration_raise_is_still_an_http_exception():
    """Existing except HTTPException guards must keep catching these."""
    error = ApiError(INVALID_CONFIGURATION, 422, "bad config")
    assert isinstance(error, HTTPException)
    assert error.status_code == 422
    assert error.detail == "bad config"
    assert str(error) == "bad config"


@pytest.mark.parametrize(
    "text",
    [
        "Global settings changed; reload before saving again",
    ],
)
def test_global_settings_conflict_carries_a_code_without_the_legacy_table(text):
    from wenyi_backend.errors import code_for

    # The revision conflict raises ApiError directly, so it needs no legacy
    # mapping; pinning that keeps the table honest about what it still covers.
    assert code_for(409, text) is None
