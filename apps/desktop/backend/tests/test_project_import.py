"""Explicit current-format imports never become old-data migration or discovery."""

import json

import pytest
from catalog_support import orphan
from fastapi.testclient import TestClient
from wenyi_desktop import paths, project_import
from wenyi_desktop.main import create_app, create_context


@pytest.mark.parametrize("done", [False, True])
def test_import_restart_and_delete_preserve_project_identity_and_state(tmp_path, done):
    directory, source, digest = orphan(tmp_path / "Downloads", done=done)
    pid = directory.name
    state = (directory / "state.sqlite3").read_bytes()
    root = tmp_path / "data"
    with TestClient(create_app(root)) as client:
        answer = client.post("/desktop/projects/import", json={"path": str(directory)})
        assert answer.status_code == 201, answer.text
        assert answer.json()["id"] == pid
        assert answer.json()["source_sha256"] == digest
        assert answer.json()["status"] == ("done" if done else "prepared")
        assert not directory.exists()
    destination = paths.project(root, pid)
    assert (destination / source.name).exists()
    assert (destination / "state.sqlite3").read_bytes() == state
    with TestClient(create_app(root)) as client:
        assert client.get("/projects").json()[0]["id"] == pid
        assert client.delete(f"/projects/{pid}").status_code == 200
    assert not destination.exists()


@pytest.mark.parametrize("marker", [None, {}, {"schema_version": 1, "pid": "a" * 16}])
def test_old_or_missing_markers_are_refused_without_touching_originals(tmp_path, marker):
    directory, source, _ = orphan(tmp_path / "Downloads")
    if marker is None:
        paths.marker_in(directory).unlink()
    else:
        paths.marker_in(directory).write_text(json.dumps(marker))
    before = source.read_bytes(), (directory / "state.sqlite3").read_bytes()
    with TestClient(create_app(tmp_path / "data")) as client:
        response = client.post("/desktop/projects/import", json={"path": str(directory)})
        assert response.status_code == 422
        assert response.json()["code"] == "project_import_failed"
        assert "Old projects are not migrated" in response.json()["message"]
        assert client.get("/projects").json() == []
    assert (source.read_bytes(), (directory / "state.sqlite3").read_bytes()) == before


@pytest.mark.parametrize("issue", ["changed", "missing", "ambiguous"])
def test_source_identity_is_required_before_any_copy(tmp_path, issue):
    directory, source, _ = orphan(tmp_path / "Downloads")
    if issue == "changed":
        source.write_text("Different source")
    elif issue == "missing":
        source.unlink()
    else:
        (directory / "source-second.txt").write_bytes(source.read_bytes())
    with TestClient(create_app(tmp_path / "data")) as client:
        response = client.post("/desktop/projects/import", json={"path": str(directory)})
        assert response.status_code == 422
        assert response.json()["code"] == "project_import_failed"
        assert response.json()["message"]
        assert directory.exists()
        assert client.get("/projects").json() == []


def test_duplicate_import_is_refused_and_failed_copy_keeps_original(tmp_path, monkeypatch):
    directory, source, _ = orphan(tmp_path / "Downloads")
    context = create_context(tmp_path / "data")
    original = source.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("Simulated copy failure")

    monkeypatch.setattr(project_import.shutil, "copytree", fail)
    with pytest.raises(project_import.ImportRefused):
        project_import.import_project(context.repository, directory)
    assert source.read_bytes() == original
    assert context.repository.list_projects() == []
    context.repository.close()


def test_catalog_failure_keeps_the_import_original(tmp_path, monkeypatch):
    directory, source, _ = orphan(tmp_path / "Downloads")
    backend = create_context(tmp_path / "data").repository
    original = source.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("Simulated catalog failure")

    monkeypatch.setattr(backend, "create_project", fail)
    try:
        with pytest.raises(OSError, match="catalog failure"):
            project_import.import_project(backend, directory)
        assert source.read_bytes() == original
    finally:
        backend.close()


@pytest.mark.parametrize("entry", ["project.json", "state.sqlite3", "cache/nested"])
def test_linked_files_are_refused_before_reading_project_metadata(tmp_path, monkeypatch, entry):
    directory, _, _ = orphan(tmp_path / "Downloads")
    linked = directory / entry
    linked.parent.mkdir(parents=True, exist_ok=True)
    if linked.exists():
        linked.rename(tmp_path / linked.name)
    else:
        (tmp_path / linked.name).write_text("Not project data")
    try:
        linked.symlink_to(tmp_path / linked.name)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    backend = create_context(tmp_path / "data").repository

    def forbidden(*args, **kwargs):
        raise AssertionError("Linked project metadata was opened")

    monkeypatch.setattr(project_import, "scan_project", forbidden)
    try:
        with pytest.raises(project_import.ImportRefused):
            project_import.import_project(backend, directory)
        assert directory.exists()
    finally:
        backend.close()
