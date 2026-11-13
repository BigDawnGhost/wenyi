"""A saved library root owns the project even when another root has a stale copy."""

import json
import shutil
from pathlib import Path

import pytest
from catalog_support import orphan
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_backend.context import use_context
from wenyi_backend.errors import ApiError
from wenyi_backend.execution.bindings import resolve_source
from wenyi_desktop import paths, project_import
from wenyi_desktop.main import create_app, create_context

PROJECT = "0123456789abcdef"


def test_project_list_scans_each_library_once_and_rechecks_next_time(desktop_context, monkeypatch):
    backend = desktop_context.repository
    identities = [backend.create_project(f"Book {index}", "en", "zh", {}) for index in range(10)]
    for pid in identities:
        backend.storage_for(pid).close()
    root = paths.projects(backend.workspace)
    scans = []
    iterdir = Path.iterdir

    def tracked(path):
        if path == root:
            scans.append(path)
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", tracked)
    assert all(row["available"] for row in backend.list_projects())
    assert scans == [root]
    # No directory result may survive this request: even an otherwise valid
    # second claim must make the next list refuse this identity, not every book.
    directory = root / identities[0]
    shutil.copytree(directory, directory.with_name(f"{identities[0]}-Copy"))
    scans.clear()
    listed = {row["id"]: row for row in backend.list_projects()}
    assert scans == [root]
    assert listed[identities[0]]["available"] is False
    assert all(listed[pid]["available"] for pid in identities[1:])


def project_with_stale_copy(tmp_path, *, stale_name="Original"):
    owner, other = tmp_path / "Owner", tmp_path / "Other"
    context = create_context(tmp_path / "workspace", library_root=other)
    backend = context.repository
    backend.create_project("Original", "en", "zh", {}, project_id=PROJECT, library_root=owner)
    backend.storage_for(PROJECT).close()
    directory = backend.project_dir(PROJECT)
    source = directory / "source-book.txt"
    source.write_text("Original content.", encoding="utf-8")
    backend.set_project_source(PROJECT, source.name, "Original", fmt="text")
    stale = paths.project_in(other, PROJECT, stale_name)
    shutil.copytree(directory, stale)
    return context, directory, stale


@pytest.mark.parametrize("stale_name", ["Original", "Older title"])
def test_missing_owner_never_uses_or_deletes_a_copy_in_another_root(tmp_path, stale_name):
    context, directory, stale = project_with_stale_copy(tmp_path, stale_name=stale_name)
    backend = context.repository
    directory.parent.rename(tmp_path / "Disconnected drive")

    assert backend.project_dir(PROJECT) == directory
    assert backend.get_project(PROJECT)["available"] is False
    with pytest.raises(ValueError, match="source file is missing"):
        resolve_source(context, PROJECT)
    with pytest.raises(HTTPException) as error:
        backend.project_directories()
    assert error.value.status_code == 409
    assert str(directory) in error.value.detail
    with use_context(context):
        backend.remove_project(PROJECT)
    assert backend.get_project(PROJECT) is None
    assert not directory.parent.exists()
    assert (stale / "source-book.txt").read_text(encoding="utf-8") == "Original content."
    assert not paths.deleting_marker_in(stale).exists()


def test_writable_open_does_not_recreate_a_missing_uploaded_project(tmp_path):
    context, directory, stale = project_with_stale_copy(tmp_path)
    backend = context.repository
    directory.parent.rename(tmp_path / "Disconnected drive")
    with pytest.raises(ApiError) as error:
        backend.storage_for(PROJECT).close()
    assert error.value.code == "project_location_unavailable"
    assert not directory.parent.exists()
    assert backend.get_project(PROJECT)["available"] is False
    with pytest.raises(ApiError):
        backend.project_directories()
    assert (stale / "source-book.txt").read_text(encoding="utf-8") == "Original content."


@pytest.mark.parametrize("activity", ["job", "download"])
def test_missing_project_removal_still_refuses_active_work(tmp_path, activity):
    context, directory, _ = project_with_stale_copy(tmp_path)
    backend = context.repository
    if activity == "job":
        backend.create_job(PROJECT, "translation", "task-1", status="running")
    else:
        identity = backend.create_export(PROJECT, "txt", {})
        output = directory / "exports" / "translated.txt"
        output.parent.mkdir()
        output.write_text("Translation.", encoding="utf-8")
        backend.publish_export(PROJECT, identity, str(output))
        # Model a still-open download without relying on Windows allowing an
        # open file's parent directory to be moved by the fixture.
        backend._export_leases[identity] = 1
    directory.parent.rename(tmp_path / "Disconnected drive")
    try:
        with use_context(context), pytest.raises(HTTPException) as error:
            backend.remove_project(PROJECT)
        assert error.value.status_code == 409
        assert backend._get("projects", PROJECT) is not None
        assert not directory.parent.exists()
    finally:
        backend._export_leases.clear()


def test_catalog_root_change_invalidates_a_previously_found_directory(tmp_path):
    context, directory, stale = project_with_stale_copy(tmp_path)
    backend = context.repository
    assert backend.project_dir(PROJECT) == directory
    # An explicit relocation updates the row. The formerly found path still
    # exists, so an existence-only cache would silently ignore the new owner.
    backend.update_project(PROJECT, library_root=str(stale.parent))
    assert backend.project_dir(PROJECT) == stale
    assert Path(resolve_source(context, PROJECT)).parent == stale
    assert backend.project_directories() == (stale.resolve(),)
    with use_context(context):
        backend.remove_project(PROJECT)
    assert directory.is_dir()
    assert not stale.exists()


def test_same_root_rename_wins_over_a_stale_copy_with_the_old_title(tmp_path):
    context, directory, stale = project_with_stale_copy(tmp_path)
    renamed = directory.with_name("Renamed within the owner")
    directory.rename(renamed)
    backend = context.repository
    assert backend.project_dir(PROJECT) == renamed
    assert backend.project_directories() == (renamed.resolve(),)
    assert Path(resolve_source(context, PROJECT)).parent == renamed
    with use_context(context):
        backend.remove_project(PROJECT)
    assert not renamed.exists()
    assert stale.is_dir()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("issue", ["duplicate", "conflicting_marker"])
def test_ambiguous_or_conflicting_owner_is_refused_for_every_operation(tmp_path, legacy, issue):
    context, directory, stale = project_with_stale_copy(tmp_path)
    backend = context.repository
    if legacy:
        backend.update_project(PROJECT, library_root=None)
    if issue == "duplicate":
        # The exact title and an already-found directory must not win merely
        # because another matching identity sorts later.
        shutil.copytree(directory, directory.with_name(f"{PROJECT}-Copy"))
    else:
        paths.marker_in(directory).write_text(json.dumps({"pid": "f" * 16}), encoding="utf-8")
    for operation in (
        lambda: backend.project_dir(PROJECT),
        lambda: resolve_source(context, PROJECT),
        backend.project_directories,
        lambda: backend.remove_project(PROJECT),
    ):
        with use_context(context), pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 409
    assert directory.is_dir()
    assert stale.is_dir()
    assert backend._get("projects", PROJECT) is not None


def test_rows_without_an_owner_are_refused_without_discovery(tmp_path):
    context, directory, stale = project_with_stale_copy(tmp_path)
    backend = context.repository
    backend.update_project(PROJECT, library_root=None)
    for operation in (
        lambda: backend.project_dir(PROJECT),
        lambda: resolve_source(context, PROJECT),
        backend.project_directories,
        lambda: backend.remove_project(PROJECT),
    ):
        with use_context(context), pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 409
        assert "root" in error.value.detail.lower()
    assert directory.is_dir() and stale.is_dir()


def test_an_unadmitted_recorded_root_cannot_fall_back_to_an_admitted_copy(tmp_path):
    context, directory, stale = project_with_stale_copy(tmp_path)
    backend = context.repository
    backend.update_project(PROJECT, library_root=str(tmp_path / "Unadmitted"))
    for operation in (
        lambda: backend.project_dir(PROJECT),
        backend.project_directories,
        lambda: backend.remove_project(PROJECT),
    ):
        with use_context(context), pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 409
    assert directory.is_dir() and stale.is_dir()


@pytest.mark.parametrize("issue", ["duplicate", "conflicting_marker", "unadmitted"])
def test_unsafe_project_does_not_hide_healthy_projects_in_the_list(tmp_path, issue):
    context, directory, stale = project_with_stale_copy(tmp_path)
    backend = context.repository
    healthy = backend.create_project("Healthy", "en", "zh", {})
    backend.storage_for(healthy).close()
    if issue == "duplicate":
        shutil.copytree(directory, directory.with_name(f"{PROJECT}-Copy"))
    elif issue == "conflicting_marker":
        paths.marker_in(directory).write_text(json.dumps({"pid": "f" * 16}), encoding="utf-8")
    else:
        backend.update_project(PROJECT, library_root=str(tmp_path / "Unadmitted"))

    with TestClient(create_app(context=context)) as client:
        response = client.get("/projects")
        assert response.status_code == 200, response.text
        listed = {project["id"]: project for project in response.json()}
        assert listed[healthy]["available"] is True
        assert listed[healthy]["error"] is None
        assert listed[PROJECT]["available"] is False
        assert listed[PROJECT]["error"]
        assert backend._get("projects", PROJECT)["error"] is None
        refused = client.post(f"/projects/{PROJECT}/prepare")
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "project_location_unavailable"
    assert directory.is_dir() and stale.is_dir()


def test_unrelated_api_errors_are_not_hidden_as_location_failures(tmp_path, monkeypatch):
    context, _, _ = project_with_stale_copy(tmp_path)
    backend = context.repository

    def fail(*args, **kwargs):
        raise ApiError("unrelated_error", 409, "Unrelated failure")

    monkeypatch.setattr(backend, "storage_for", fail)
    with pytest.raises(ApiError) as error:
        backend.get_project(PROJECT)
    assert error.value.code == "unrelated_error"


def test_startup_does_not_adopt_unregistered_projects_from_saved_library_roots(tmp_path):
    first, _, _ = orphan(tmp_path / "First")
    second = tmp_path / "Second" / "projects" / f"{first.name}-Copy"
    shutil.copytree(first, second)
    healthy, _, _ = orphan(tmp_path / "First", "b" * 16)
    context = create_context(tmp_path / "workspace")
    backend = context.repository
    with backend.transaction() as connection:
        backend.save_settings(
            {
                "config": {},
                "template": "标准翻译",
                "revision": 0,
                paths.LIBRARY_ROOTS: [str(first.parent), str(second.parent)],
            },
            connection,
        )
    before = {folder: (folder / "state.sqlite3").read_bytes() for folder in (first, second)}
    with TestClient(create_app(context=context)) as client:
        assert client.get("/projects").json() == []
    assert healthy.is_dir()
    assert {folder: (folder / "state.sqlite3").read_bytes() for folder in before} == before


@pytest.mark.parametrize(
    "marker",
    [
        "not JSON",
        "[]",
        "{}",
        '{"pid": ""}',
        '{"pid": "invalid"}',
        '{"pid": "ffffffffffffffff"}',
    ],
)
def test_import_refuses_invalid_identity_before_copying_or_removing(tmp_path, marker):
    directory, source, _ = orphan(tmp_path / "Downloads")
    paths.marker_in(directory).write_text(marker, encoding="utf-8")
    original_state = (directory / "state.sqlite3").read_bytes()
    original_source = source.read_bytes()
    context = create_context(tmp_path / "workspace")
    with pytest.raises(project_import.ImportRefused) as error:
        project_import.import_project(context.repository, directory)
    assert error.value.reason == "project_identity_invalid"
    assert "marker" in error.value.message.lower()
    assert context.repository.list_projects() == []
    assert not paths.projects(context.repository.workspace).exists()
    assert source.read_bytes() == original_source
    assert (directory / "state.sqlite3").read_bytes() == original_state


def test_import_preserves_marker_identity_after_a_directory_rename(tmp_path):
    directory, source, digest = orphan(tmp_path / "Downloads", done=True)
    pid = directory.name
    paths.marker_in(directory).write_text(
        json.dumps({"pid": pid, "schema_version": paths.PROJECT_SCHEMA_VERSION}), encoding="utf-8"
    )
    renamed = directory.with_name("Renamed book")
    directory.rename(renamed)
    context = create_context(tmp_path / "workspace")
    imported = project_import.import_project(context.repository, renamed)
    assert imported["id"] == pid
    assert imported["source_sha256"] == digest
    assert imported["status"] == "done"
    assert Path(resolve_source(context, pid)).name == source.name
    assert not renamed.exists()


def test_startup_leaves_unregistered_projects_untouched_regardless_of_marker_validity(tmp_path):
    workspace = tmp_path / "workspace"
    invalid, _, _ = orphan(workspace)
    healthy, _, _ = orphan(workspace, "b" * 16)
    paths.marker_in(invalid).write_text(json.dumps({"pid": "f" * 16}), encoding="utf-8")
    context = create_context(workspace)
    with TestClient(create_app(context=context)) as client:
        assert client.get("/projects").json() == []
    assert healthy.is_dir() and invalid.is_dir()
