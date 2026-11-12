"""Newer project schemas remain isolated from other projects and HTTP failures."""

import sqlite3

import pytest
from desktop_test_support import initialize
from wenyi_backend.errors import PROJECT_BUSY, ApiError
from wenyi_core.storage.sqlite import SCHEMA_VERSION
from wenyi_desktop.local_backend import PROJECT_SCHEMA_TOO_NEW


def newer_project(backend):
    pid, storage = initialize(backend)
    storage.close()
    database = backend.project_dir(pid) / "state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    return pid, database


def test_newer_project_schema_does_not_break_the_project_list(desktop):
    client, backend, _ = desktop
    healthy, storage = initialize(backend)
    storage.close()
    newer, database = newer_project(backend)

    response = client.get("/projects")
    assert response.status_code == 200, response.text
    projects = {project["id"]: project for project in response.json()}
    assert projects[healthy]["available"] is True
    assert projects[newer]["available"] is False
    assert client.get(f"/projects/{healthy}").status_code == 200
    assert backend._get("projects", newer) is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_newer_project_schema_has_the_same_error_on_eager_and_lazy_open(desktop):
    client, backend, _ = desktop
    pid, _ = newer_project(backend)

    with pytest.raises(ApiError) as caught:
        backend.storage_for(pid)
    assert caught.value.status_code == 409
    assert caught.value.code == PROJECT_SCHEMA_TOO_NEW
    for path in (f"/projects/{pid}", f"/projects/{pid}/preview"):
        response = client.get(path)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == PROJECT_SCHEMA_TOO_NEW
        assert "newer version" in response.json()["message"]


def test_project_discovery_does_not_hide_unrelated_api_errors(desktop_context, monkeypatch):
    backend = desktop_context.repository
    pid = backend.create_project("Busy", "en", "zh", {})

    def busy(*args, **kwargs):
        raise ApiError(PROJECT_BUSY, 409, "Synthetic contention")

    monkeypatch.setattr(backend, "storage_for", busy)
    with pytest.raises(ApiError) as caught:
        backend.get_project(pid)
    assert caught.value.code == PROJECT_BUSY
