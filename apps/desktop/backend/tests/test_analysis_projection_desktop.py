import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from desktop_test_support import initialize
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.fixture
def analysis_project(desktop_context):
    pid, storage = initialize(desktop_context.repository)
    chapter = Chapter(
        index=8,
        title="Stored title",
        meta={"source_digest": "摘要"},
        segments=[Segment(index=2, source="Body")],
    )
    storage.save_chapter(chapter)
    storage.save_chapter(Chapter(index=13, title="Not in manifest"))
    manifest = storage.load_manifest()
    manifest["chapters"].insert(0, {"index": 8, "title": "Old manifest title"})
    storage.save_manifest(manifest)
    storage.close()
    with TestClient(create_app(desktop_context)) as client:
        yield client, f"/projects/{pid}", storage
    storage.close()


@pytest.mark.parametrize("uncached", [False, True])
def test_desktop_analysis_reads_only_missing_compact_rows(analysis_project, monkeypatch, uncached):
    client, root, storage = analysis_project
    if uncached:
        # Generic writes invalidate the compact row; older saved projects may lack it.
        storage.write_artifact("chapters/ch8.json", storage.load_chapter(8).model_dump())
        storage.close()

    body_reads = []
    read_artifact = SqliteStorage.read_artifact

    def track(store, key):
        if key.startswith("chapters/"):
            body_reads.append(key)
        return read_artifact(store, key)

    def forbid_counts(chapter):
        pytest.fail("Analysis must not count paragraphs even during the fallback")

    monkeypatch.setattr(SqliteStorage, "read_artifact", track)
    monkeypatch.setattr(Chapter, "text_segments", property(forbid_counts))
    response = client.get(root + "/analysis")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "analysis": {},
        "chapter_digests": [
            {"index": 8, "title": "Stored title", "digest": "摘要"},
            {"index": 0, "title": "Chapter", "digest": ""},
        ],
    }
    assert body_reads == (["chapters/ch8.json"] if uncached else [])


def test_desktop_analysis_projection_accepts_old_state_without_upgrade(analysis_project):
    _, _, storage = analysis_project
    with closing(sqlite3.connect(storage.database_path)) as conn, conn:
        conn.execute("PRAGMA user_version=1")
        conn.execute("UPDATE chapter_statistics SET title='Stale'")

    with closing(SqliteStorage(storage.run_dir, create=False)) as reader:
        assert reader.read_chapter_digests() == [(8, "Stored title", "摘要"), (0, "Chapter", None)]

    with closing(sqlite3.connect(storage.database_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)


def test_desktop_analysis_does_not_hide_missing_chapter(analysis_project):
    client, root, storage = analysis_project
    storage.delete_artifact("chapters/ch8.json")
    storage.close()

    response = client.get(root + "/analysis")
    assert response.status_code == 404
    assert response.json()["code"] == "chapter_not_found"
    assert response.json()["message"] == "chapter 8 not found"


def test_desktop_analysis_keeps_unavailable_project_error(analysis_project, tmp_path):
    client, root, storage = analysis_project
    Path(storage.run_dir).rename(tmp_path / "disconnected")

    response = client.get(root + "/analysis")
    assert response.status_code == 409
    assert response.json()["code"] == "project_location_unavailable"
