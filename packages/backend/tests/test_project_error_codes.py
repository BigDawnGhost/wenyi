"""Migrated project raises must carry codes without changing their text."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from wenyi_backend import dal
from wenyi_backend.application import create_app
from wenyi_backend.context import use_context
from wenyi_backend.errors import PROJECT_BUSY, PROJECT_NOT_FOUND, ApiError
from wenyi_backend.project_service import project_write


def test_missing_project_reports_project_not_found(backend_context, monkeypatch):
    monkeypatch.setattr(dal, "get_project", lambda pid: None)
    client = TestClient(create_app(backend_context))
    response = client.get("/projects/absent")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == PROJECT_NOT_FOUND
    assert body["detail"] == "project not found"
    assert body["message"] == "project not found"


def _open_lock():
    from contextlib import contextmanager

    @contextmanager
    def lock(blocking=True):
        yield None

    return lock


def test_busy_project_write_raises_project_busy(backend_context, monkeypatch):
    """The write boundary is the guard every state-changing route passes through."""
    project = {"id": "book", "fmt": "epub", "status": "translating", "initialized": True}
    monkeypatch.setattr(dal, "get_project", lambda pid: project)

    class OpenStorage:
        lock = staticmethod(_open_lock())

        def close(self):
            pass

    context = replace(backend_context, storage_for=lambda pid: OpenStorage())
    with use_context(context):
        with pytest.raises(ApiError) as caught:
            with project_write("book"):
                pytest.fail("busy project must not enter the write section")
    assert caught.value.code == PROJECT_BUSY
    assert caught.value.status == 409
    assert caught.value.message == "project already has a running task"


def test_lock_contention_raises_project_busy(backend_context, monkeypatch):
    project = {"id": "book", "fmt": "epub", "status": "prepared", "initialized": True}
    monkeypatch.setattr(dal, "get_project", lambda pid: project)

    class LockedStorage:
        def lock(self, blocking=True):
            raise BlockingIOError("locked")

        def close(self):
            pass

    context = replace(backend_context, storage_for=lambda pid: LockedStorage())
    with use_context(context):
        with pytest.raises(ApiError) as caught:
            with project_write("book"):
                pytest.fail("contended lock must not enter the write section")
    assert caught.value.code == PROJECT_BUSY
    assert caught.value.message == "project already has a running task"
