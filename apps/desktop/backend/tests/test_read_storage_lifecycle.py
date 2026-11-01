"""HTTP inspection closes owned SQLite connections without relying on garbage collection."""

import pytest
from desktop_test_support import initialize
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.fixture
def inspected_project(desktop_context, monkeypatch):
    pid, storage = initialize(desktop_context.repository)
    storage.write_artifact("reviews/review-audit/result.json", {"status": "completed"})
    storage.close()
    instances = []
    initialize_storage = SqliteStorage.__init__

    def track(storage, *args, **kwargs):
        initialize_storage(storage, *args, **kwargs)
        instances.append(storage)

    monkeypatch.setattr(SqliteStorage, "__init__", track)
    try:
        with TestClient(create_app(desktop_context)) as client:
            yield pid, client, instances
    finally:
        for instance in instances:
            instance.close()


@pytest.mark.parametrize(
    "path,status",
    [
        ("", 200),
        ("/chapters", 200),
        ("/chapters/0", 200),
        ("/chapters/99", 404),
        ("/chapters/0/segments/0/precision-drafts", 200),
        ("/review/0", 200),
        ("/review/0/segments/0/history", 200),
        ("/review/runs", 200),
        ("/review/runs/review-missing", 404),
        ("/glossary/terms", 200),
        ("/glossary/terms/page", 200),
        ("/glossary/conflicts", 200),
        ("/glossary/export", 200),
        ("/events", 200),
        ("/analysis", 200),
        ("/stats", 200),
    ],
)
def test_inspection_releases_all_storage_handles(inspected_project, path, status):
    pid, client, instances = inspected_project
    response = client.get(f"/projects/{pid}{path}")
    assert response.status_code == status, response.text
    assert instances, "The route must actually inspect persisted state"
    assert all(instance._conn is None for instance in instances)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_retired_report_does_not_open_storage(inspected_project, method):
    pid, client, instances = inspected_project
    assert client.request(method, f"/projects/{pid}/report").status_code == 404
    assert instances == []


def test_read_failure_still_closes_storage(inspected_project, monkeypatch):
    pid, client, instances = inspected_project

    def fail(storage):
        raise OSError("Read failed")

    monkeypatch.setattr(SqliteStorage, "all_terms", fail)
    with pytest.raises(OSError, match="Read failed"):
        client.get(f"/projects/{pid}/glossary/terms")
    assert all(instance._conn is None for instance in instances)


def test_subtitle_inspection_closes_storage(desktop_context, monkeypatch):
    backend = desktop_context.repository
    pid = backend.create_project("Subtitles", "en", "zh", {}, source={"fmt": "srt"})
    storage = backend.storage_for(pid)
    storage.write_artifact("subtitle_preview.json", [{"index": "1", "source": "Hello"}])
    storage.close()
    monkeypatch.setattr("wenyi_backend.routers.subtitles.storage_for", lambda _: storage)
    with TestClient(create_app(desktop_context)) as client:
        assert client.get(f"/projects/{pid}/subtitles").status_code == 200
    assert storage._conn is None
