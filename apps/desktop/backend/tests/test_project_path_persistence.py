"""Project files remain usable after settings saves, imports and path changes."""

import json
import ntpath
from dataclasses import replace
from pathlib import Path, PureWindowsPath

import pytest
from catalog_support import orphan
from desktop_test_support import wait_job
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tests.fake_llm import MeteredFakeClient, routing_handler
from wenyi_backend.config_documents import global_document
from wenyi_backend.context import use_context
from wenyi_backend.execution.bindings import resolve_source
from wenyi_backend.source_upload import UploadedSource
from wenyi_desktop import catalog_recovery, paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.main import create_app, create_context


@pytest.mark.parametrize("chosen", [False, True])
def test_settings_save_and_changed_default_keep_existing_project(tmp_path, chosen):
    workspace, profile = tmp_path / "workspace", tmp_path / "profile"
    library = tmp_path / "Library"
    context = create_context(workspace, user_home=profile, library_root=library)
    backend = context.repository
    selected = tmp_path / "Selected" if chosen else library
    pid = backend.create_project(
        "Named Book", "en", "zh", {}, **({"library_root": selected} if chosen else {})
    )
    backend.storage_for(pid).close()
    directory = backend.project_dir(pid)
    settings = context.settings_store.load()
    with use_context(context):
        context.settings_store.save(
            json.dumps(global_document(settings.config)),
            settings.default_template,
            settings.revision,
        )
    saved = backend.load_settings()
    assert saved[paths.LAST_LIBRARY_ROOT] == str(selected)
    assert str(selected) in saved[paths.LIBRARY_ROOTS]
    backend.close()

    restarted = create_context(
        workspace, user_home=profile, library_root=tmp_path / "New installation library"
    )
    try:
        assert restarted.repository.project_dir(pid) == directory
        assert restarted.repository.get_project(pid)["available"] is True
        assert selected in restarted.repository.project_roots()
    finally:
        restarted.repository.close()


@pytest.mark.parametrize("location", ["default", "custom"])
def test_fresh_library_upload_parse_restart_and_delete_use_one_directory(tmp_path, location):
    workspace = tmp_path / "workspace"
    library = tmp_path / "Library"
    context = create_context(workspace, library_root=library)
    backend = context.repository
    chosen = tmp_path / "Chosen" if location == "custom" else library
    body = {"name": "Named Book", "source_lang": "en", "target_lang": "zh"}
    resolutions = []

    def resolve_directory(pid, name, location):
        assert not chosen.exists(), "The host must resolve the directory before writing bytes"
        resolutions.append((pid, name, location))
        return backend.new_project_dir(pid, name, location)

    context = replace(context, new_project_dir=resolve_directory)
    if location == "custom":
        body["location"] = str(chosen)
    assert not paths.projects(workspace).exists()
    with use_context(context), TestClient(create_app(context=context)) as client:
        created = client.post(
            "/projects",
            data={"project": json.dumps(body)},
            files={"file": ("book.txt", b"A short paragraph.\n", "text/plain")},
        )
        assert created.status_code == 201, created.text
        pid = created.json()["id"]
        assert resolutions == [(pid, body["name"], body.get("location"))]
        job = wait_job(backend.list_jobs(pid)[0]["run_id"])
        assert job["status"] == "done", job
        directory = chosen / paths.project_name(pid, body["name"])
        assert backend.project_dir(pid) == directory
        source = Path(resolve_source(context, pid))
        assert source.parent == directory
        assert (directory / "state.sqlite3").is_file()
        assert set(chosen.iterdir()) == {directory}
        assert client.get(f"/projects/{pid}/preview").status_code == 200

    restarted = create_context(workspace, library_root=library)
    with TestClient(create_app(context=restarted)) as client:
        assert Path(resolve_source(restarted, pid)) == source
        assert client.delete(f"/projects/{pid}").status_code == 200
    assert not directory.exists()
    assert not list(chosen.rglob("source-*"))


def test_desktop_source_identity_does_not_compute_a_cross_volume_relative_path(tmp_path):
    context = create_context(tmp_path / "workspace")
    source = PureWindowsPath(r"D:\Books\0123456789abcdef-Named_Book\source-book.txt")
    # This is the operation that fails on a Windows C: workspace + D: library.
    with pytest.raises(ValueError):
        ntpath.relpath(source, r"C:\Wenyi\projects")
    with use_context(context):
        fields = UploadedSource(source, "book.txt", "text", "a" * 64).fields()
    assert fields["source_path"] == source.name


@pytest.mark.parametrize("rename", ["title", "suffix", "marker"])
def test_project_lookup_and_source_identity_survive_renaming(tmp_path, rename):
    workspace, profile, library = (
        tmp_path / "workspace",
        tmp_path / "profile",
        tmp_path / "Library",
    )
    context = create_context(workspace, user_home=profile, library_root=library)
    backend = context.repository
    pid = backend.create_project("Named Book", "en", "zh", {})
    backend.storage_for(pid).close()
    directory = backend.project_dir(pid)
    source = directory / "source-book.txt"
    source.write_text("A short paragraph.\n", encoding="utf-8")
    backend.set_project_source(pid, str(source), "Named Book", fmt="text")
    if rename == "title":
        backend.update_project(pid, name="Changed title")
    else:
        renamed = directory.with_name(f"{pid}-Renamed" if rename == "suffix" else "Renamed book")
        directory.rename(renamed)
        directory = renamed
    backend.close()

    restarted = create_context(workspace, user_home=profile, library_root=library)
    assert restarted.repository.project_dir(pid) == directory
    assert Path(resolve_source(restarted, pid)) == directory / source.name
    assert restarted.repository.project_directories() == (directory.resolve(),)
    restarted.repository.close()


def test_imported_source_runs_after_original_directory_is_removed(tmp_path):
    directory, source, digest = orphan(tmp_path / "Downloads", done=True)
    pid = directory.name
    original_state = (directory / "state.sqlite3").read_bytes()
    fake = MeteredFakeClient(routing_handler)
    context = replace(create_context(tmp_path / "workspace"), build_client=lambda config: fake)
    with use_context(context), TestClient(create_app(context=context)) as client:
        response = client.post("/desktop/projects/import", json={"path": str(directory)})
        assert response.status_code == 201, response.text
        assert not directory.exists()
        stored_source = Path(resolve_source(context, pid))
        assert stored_source.name == source.name
        assert stored_source.parent == context.repository.project_dir(pid)
        assert (stored_source.parent / "state.sqlite3").read_bytes() == original_state
        assert context.repository.get_project(pid)["source_sha256"] == digest
        started = client.post(f"/projects/{pid}/prepare")
        assert started.status_code == 200, started.text
        job = wait_job(started.json()["job_id"])
        assert job["status"] == "done", job
        storage = context.storage_for(pid)
        try:
            assert storage.load_manifest()["source_sha256"] == digest
            assert storage.load_chapter(0).segments[0].target == "一小段原文。"
        finally:
            storage.close()


def test_import_refuses_changed_source_before_moving_or_rewriting_state(tmp_path):
    directory, source, _ = orphan(tmp_path / "Downloads", done=True)
    source.write_text("Changed outside Wenyi.\n", encoding="utf-8")
    before = (directory / "state.sqlite3").read_bytes()
    context = create_context(tmp_path / "workspace")
    with use_context(context), TestClient(create_app(context=context)) as client:
        response = client.post("/desktop/projects/import", json={"path": str(directory)})
        assert response.status_code == 422, response.text
        assert "checksum" in response.json()["message"]
        assert context.repository.list_projects() == []
    assert source.read_text(encoding="utf-8") == "Changed outside Wenyi.\n"
    assert (directory / "state.sqlite3").read_bytes() == before


def test_recovery_uses_every_saved_root_and_keeps_external_source_resolvable(tmp_path):
    first, _, _ = orphan(tmp_path / "First")
    second, source, _ = orphan(tmp_path / "Second", "b" * 16)
    context = create_context(tmp_path / "workspace")
    backend = context.repository
    with backend.transaction() as connection:
        backend.save_settings(
            {
                "config": {},
                "template": "标准翻译",
                "revision": 0,
                paths.LIBRARY_ROOTS: [str(first.parent), str(second.parent)],
                paths.LAST_LIBRARY_ROOT: str(first.parent),
            },
            connection,
        )
    report = catalog_recovery.recover_empty_catalog(backend)
    assert report.recovered == 2
    assert backend.project_dir(second.name) == second
    assert Path(resolve_source(context, second.name)) == source
    assert backend.get_project(second.name)["available"] is True


def test_backup_directory_enumeration_refuses_a_missing_registered_project(tmp_path):
    backend = LocalBackend(tmp_path / "workspace")
    pid = backend.create_project("Missing", "en", "zh", {})
    with pytest.raises(HTTPException) as error:
        backend.project_directories()
    assert error.value.status_code == 409
    assert pid in error.value.detail
    assert "Reconnect" in error.value.detail
    backend.storage_for(pid).close()
    assert backend.project_directories() == (backend.project_dir(pid).resolve(),)


def test_backup_directory_enumeration_never_includes_a_selected_parent(tmp_path):
    backend = LocalBackend(tmp_path / "workspace")
    selected = tmp_path / "Books"
    pid = backend.create_project("Owned", "en", "zh", {}, library_root=selected)
    backend.storage_for(pid).close()
    unrelated = selected / "Personal notes.txt"
    unrelated.write_text("Not a project.", encoding="utf-8")
    assert backend.project_directories() == (backend.project_dir(pid).resolve(),)
    assert selected not in backend.project_directories()


def test_backup_directory_enumeration_refuses_a_conflicting_marker(tmp_path):
    backend = LocalBackend(tmp_path / "workspace")
    pid = backend.create_project("Owned", "en", "zh", {})
    backend.storage_for(pid).close()
    directory = backend.project_dir(pid)
    paths.marker_in(directory).write_text(json.dumps({"pid": "a" * 16}), encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        backend.project_directories()
    assert error.value.status_code == 409
    assert "does not belong" in error.value.detail
    assert directory.is_dir()


@pytest.mark.parametrize(
    "reference",
    [
        "source-book.txt",
        "0123456789abcdef/source-book.txt",
        "../Old library/0123456789abcdef-Book/source-book.txt",
        r"D:\Old library\0123456789abcdef-Book\source-book.txt",
    ],
)
def test_legacy_source_references_follow_the_owning_project(tmp_path, reference):
    context = create_context(tmp_path / "workspace", library_root=tmp_path / "Library")
    backend = context.repository
    pid = backend.create_project("Book", "en", "zh", {}, project_id="0123456789abcdef")
    backend.storage_for(pid).close()
    source = backend.project_dir(pid) / "source-book.txt"
    source.write_text("Unchanged content.", encoding="utf-8")
    backend.set_project_source(pid, reference, "Book")
    assert Path(resolve_source(context, pid)) == source


def test_legacy_index_uses_the_injected_profile_and_preserves_owned_directories(tmp_path):
    directory, source, digest = orphan(tmp_path / "Old workspace")
    home = tmp_path / "Different profile"
    workspace = tmp_path / "Current workspace"
    backend = LocalBackend(workspace, user_home=home)
    pid = backend.create_project(
        "Book",
        "en",
        "zh",
        {},
        project_id=directory.name,
        source={"source_path": str(source), "source_sha256": digest},
    )
    # A migrated pre-library catalog did not record its root on each row.
    backend.update_project(pid, library_root=None)
    index = paths.user_workspace_index(home=home)
    index.write_text(
        json.dumps({"schema_version": 1, "projects": [{"id": pid, "path": str(directory)}]}),
        encoding="utf-8",
    )
    backend.close()
    restarted = create_context(workspace, user_home=home, migrate=False)
    assert restarted.repository.project_dir(pid) == directory
    assert Path(resolve_source(restarted, pid)) == source
    assert restarted.repository.project_directories() == (directory.resolve(),)
    restarted.repository.close()
