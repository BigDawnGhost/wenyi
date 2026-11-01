"""Migrated configuration raises carry codes without changing their text."""

import pytest
from fastapi import HTTPException
from wenyi_backend.errors import INVALID_CONFIGURATION, SETTINGS_CONFLICT, ApiError


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
def test_global_settings_conflict_has_its_own_code(text):
    from wenyi_backend.errors import code_for

    # A dedicated code: settings conflict is not an invalid configuration, and
    # clients branch on the difference.
    assert code_for(409, text) == SETTINGS_CONFLICT


def test_settings_adapter_raises_its_own_code():
    """The adapter must raise SETTINGS_CONFLICT, not borrow another code."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    source = (root / "apps/api/wenyi_api/global_settings.py").read_text(encoding="utf-8")
    assert "ApiError(" in source
    assert "SETTINGS_CONFLICT," in source
    assert "Global settings changed; reload before saving again" in source
