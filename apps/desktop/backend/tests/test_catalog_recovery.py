"""Windows catalog repair uses only synthetic projects in temporary workspaces."""

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from threading import Event

import pytest
from catalog_support import orphan
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_backend.context import use_context
from wenyi_core.storage.sqlite import SqliteStorage
from wenyi_desktop import local_runtime, paths
from wenyi_desktop.local_runtime import LocalRuntime
from wenyi_desktop.main import create_app, create_context


@pytest.fixture
def windows(monkeypatch):
    # Patch the adapter's platform selector, not Python's global platform identity.
    monkeypatch.setattr(local_runtime, "platform", "win32", raising=False)


@pytest.mark.parametrize("initialized,done", [(False, False), (True, False), (True, True)])
def test_windows_startup_repairs_empty_catalog_through_http(tmp_path, windows, initialized, done):
    workspace = tmp_path / "workspace"
    directory, source, digest = orphan(workspace, initialized=initialized, done=done)
    before = (directory / "state.sqlite3").read_bytes()
    app = create_app(workspace)
    with TestClient(app) as client:
        response = client.get("/projects")
        assert response.status_code == 200
        projects = response.json()
        assert [project["id"] for project in projects] == [directory.name]
        project = app.state.backend.repository.get_project(directory.name)
        assert project["name"] == "Example"
        assert project["initialized"] == initialized
        assert project["status"] == ("done" if done else "prepared" if initialized else "uploaded")
        assert project["source_sha256"] == digest
        assert workspace / "projects" / project["source_path"] == source
        assert project["config"] == {}
        assert app.state.backend.repository.all_jobs() == []
        assert app.state.backend.repository.list_exports(directory.name) == []
    assert (directory / "state.sqlite3").read_bytes() == before
    # Repeated starts neither duplicate records nor invent scheduling history.
    with TestClient(create_app(workspace)) as client:
        assert len(client.get("/projects").json()) == 1


def test_a_directory_with_a_readable_suffix_recovers_its_identifier(tmp_path, windows):
    # This is the name the layer-three layout writes. A name-based scan must read
    # the identifier out of it rather than skip the directory, which is what the
    # old 16-hex-digit pattern did -- in silence, and only when a user had already
    # lost the catalog that would have told them a project was missing.
    workspace = tmp_path / "workspace"
    directory, _, _ = orphan(workspace)
    named = directory.with_name(paths.project_name(directory.name, "A example book"))
    directory.rename(named)
    context = create_context(workspace)
    run_recovery(context)
    assert [row["id"] for row in context.repository.list_projects()] == [directory.name]


def test_a_marker_carries_the_identifier_past_a_rename(tmp_path, windows):
    # The name is for people. The marker is what survives a directory someone
    # renamed by hand, which is the case the catalog cannot answer for.
    workspace = tmp_path / "workspace"
    directory, _, _ = orphan(workspace)
    (directory / paths.PROJECT_MARKER).write_text(
        json.dumps({"pid": directory.name, "schema_version": 1}), encoding="utf-8"
    )
    renamed = directory.with_name("renamed by hand")
    directory.rename(renamed)
    context = create_context(workspace)
    run_recovery(context)
    assert [row["id"] for row in context.repository.list_projects()] == [directory.name]


def test_a_marker_that_contradicts_its_directory_is_refused(tmp_path, windows):
    # Two sources disagreeing means the pair was assembled by hand. Rebuilding a
    # catalog entry would put a project id in front of files it does not own.
    workspace = tmp_path / "workspace"
    directory, _, _ = orphan(workspace)
    (directory / paths.PROJECT_MARKER).write_text(
        json.dumps({"pid": "0" * 16, "schema_version": 1}), encoding="utf-8"
    )
    context = create_context(workspace)
    run_recovery(context)
    assert context.repository.list_projects() == []
    assert context.repository.get_project(directory.name) is None


def run_recovery(context, *, startup=True):
    async def run():
        with use_context(context):
            runtime = LocalRuntime(context.repository, context)
            runtime.recover(startup=startup)

    asyncio.run(run())


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_other_platforms_do_not_recover(tmp_path, monkeypatch, platform):
    orphan(tmp_path)
    context = create_context(tmp_path)
    monkeypatch.setattr(local_runtime, "platform", platform, raising=False)
    run_recovery(context)
    assert context.repository.list_projects() == []


def test_nonempty_catalog_and_periodic_recovery_are_untouched(tmp_path, windows):
    directory, _, _ = orphan(tmp_path)
    context = create_context(tmp_path)
    backend = context.repository
    run_recovery(context, startup=False)
    assert backend.list_projects() == []
    pid = backend.create_project("Keep me", "en", "zh", {}, config={"custom": True})
    run_recovery(context)
    assert [row["id"] for row in backend.list_projects()] == [pid]
    assert backend.get_project_config(pid) == {"custom": True}
    assert backend.get_project(directory.name) is None


def test_bad_directories_do_not_hide_valid_recovery(tmp_path, windows):
    valid, _, _ = orphan(tmp_path)
    corrupt, _, _ = orphan(tmp_path, "c" * 16)
    (corrupt / "state.sqlite3").write_bytes(b"not a database")
    mismatch, source, _ = orphan(tmp_path, "d" * 16)
    source.write_text("Different content.", encoding="utf-8")
    ambiguous, source, _ = orphan(tmp_path, "e" * 16)
    source.with_name(f"source-{'f' * 32}.txt").write_bytes(source.read_bytes())
    context = create_context(tmp_path)
    run_recovery(context)
    assert [row["id"] for row in context.repository.list_projects()] == [valid.name]
    assert context.repository.get_project(corrupt.name) is None
    assert context.repository.get_project(mismatch.name) is None
    assert context.repository.get_project(ambiguous.name) is None


def test_recovery_matches_checkpoint_hash_not_source_filename(tmp_path, windows):
    directory, source, digest = orphan(tmp_path)
    source.with_name(f"source-{'f' * 32}.txt").write_text("Old upload.", encoding="utf-8")
    context = create_context(tmp_path)
    run_recovery(context)
    project = context.repository.get_project(directory.name)
    assert project["source_sha256"] == digest
    assert Path(project["source_path"]).name == source.name


def test_permanent_deletion_does_not_return_after_windows_restart(tmp_path, windows):
    directory, _, _ = orphan(tmp_path)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/projects").json()[0]["id"] == directory.name
        assert client.delete(f"/projects/{directory.name}").status_code == 200
        assert not directory.exists()
        assert client.get("/projects").json() == []
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/projects").json() == []


def test_failed_file_cleanup_reports_error_and_keeps_catalog_for_retry(
    tmp_path, windows, monkeypatch
):
    from wenyi_desktop import local_backend

    directory, source, _ = orphan(tmp_path)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        original = local_backend.shutil.rmtree

        def denied(path):
            raise PermissionError("Open file")

        with monkeypatch.context() as patch:
            patch.setattr(local_backend.shutil, "rmtree", denied)
            response = client.delete(f"/projects/{directory.name}")
            assert response.status_code == 409
            assert source.exists()
            assert client.get(f"/projects/{directory.name}").status_code == 200
        assert local_backend.shutil.rmtree is original
        assert client.delete(f"/projects/{directory.name}").status_code == 200
        assert not directory.exists()


def test_repair_rolls_back_all_rows_and_retries_after_interruption(tmp_path, windows, monkeypatch):
    orphan(tmp_path)
    orphan(tmp_path, "c" * 16)
    context = create_context(tmp_path)
    backend = context.repository
    original = backend.create_project

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Interrupted repair")

    with monkeypatch.context() as patch:
        patch.setattr(backend, "create_project", interrupted)
        with pytest.raises(RuntimeError, match="Interrupted repair"):
            run_recovery(context)
    assert backend.list_projects() == []
    run_recovery(context)
    assert len(backend.list_projects()) == 2


def test_deletion_waits_for_export_download_and_preserves_external_files(tmp_path, windows):
    workspace = tmp_path / "workspace"
    directory, _, _ = orphan(workspace)
    saved_copy = tmp_path / "saved.txt"
    saved_copy.write_text("Saved separately.", encoding="utf-8")
    app = create_app(workspace)
    with TestClient(app) as client:
        backend = app.state.backend.repository
        export_id = backend.create_export(directory.name, "txt", {})
        output = directory / "exports" / "translated.txt"
        output.parent.mkdir()
        output.write_text("Translation.", encoding="utf-8")
        backend.publish_export(directory.name, export_id, str(output))
        stream, _ = backend.open_export(directory.name, export_id)
        try:
            assert client.delete(f"/projects/{directory.name}").status_code == 409
            assert output.exists()
        finally:
            stream.close()
        assert client.delete(f"/projects/{directory.name}").status_code == 200
        assert not directory.exists()
        assert backend.list_exports(directory.name) == []
        assert saved_copy.read_text(encoding="utf-8") == "Saved separately."


def test_cleanup_closes_owned_sqlite_handles_and_blocks_only_its_project(
    tmp_path, windows, monkeypatch
):
    from wenyi_desktop import local_backend

    directory, _, _ = orphan(tmp_path)
    context = create_context(tmp_path)
    run_recovery(context)
    backend = context.repository
    other = backend.create_project("Other", "en", "zh", {})
    entered, release = Event(), Event()
    stores = []
    original_init = SqliteStorage.__init__
    original_remove = local_backend.shutil.rmtree

    def initialize(store, *args, **kwargs):
        original_init(store, *args, **kwargs)
        stores.append(store)

    def remove(path):
        assert stores
        assert all(getattr(store._conn_local, "conn", None) is None for store in stores)
        entered.set()
        assert release.wait(10)
        original_remove(path)

    monkeypatch.setattr(SqliteStorage, "__init__", initialize)
    monkeypatch.setattr(local_backend.shutil, "rmtree", remove)
    with use_context(context), ThreadPoolExecutor(max_workers=1) as executor:
        deletion = executor.submit(copy_context().run, backend.remove_project, directory.name)
        try:
            assert entered.wait(10)
            with pytest.raises(HTTPException) as error:
                backend.admit_job(
                    directory.name, "translation", "new", project_status="translating"
                )
            assert error.value.status_code == 409
            with pytest.raises(HTTPException) as error:
                backend.admit_export(directory.name, "txt", {}, "export", {})
            assert error.value.status_code == 409
            backend.set_project_status(other, "uploaded")
            assert backend.get_project(other)["status"] == "uploaded"
        finally:
            release.set()
        deletion.result(10)
    assert backend.get_project(directory.name) is None


def test_recovery_reads_committed_wal_without_changing_project_metadata(tmp_path, windows):
    directory, _, _ = orphan(tmp_path)
    storage = SqliteStorage(str(directory))
    try:
        manifest = storage.load_manifest()
        manifest["title"] = "Committed in WAL"
        storage.save_manifest(manifest)
        context = create_context(tmp_path)
        run_recovery(context)
        assert context.repository.get_project(directory.name)["name"] == "Committed in WAL"
        assert storage.load_manifest() == manifest
    finally:
        storage.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows paths")
def test_verbatim_workspace_recovers_projects_and_preserves_translations(tmp_path, windows):
    workspace = tmp_path / "Wenyi 工作区 # 100%"
    directory, source, digest = orphan(workspace, done=True)
    before = (directory / "state.sqlite3").read_bytes()
    # Rust canonicalize() used this spelling for every --data-dir in 1.2.5.
    verbatim = Path("\\\\?\\" + str(workspace))
    for path in (verbatim, workspace):
        app = create_app(path)
        with TestClient(app) as client:
            response = client.get("/projects")
            assert response.status_code == 200
            assert [project["id"] for project in response.json()] == [directory.name]
            backend = app.state.backend.repository
            project = backend.get_project(directory.name)
            assert project["status"] == "done"
            assert project["source_sha256"] == digest
            assert backend.workspace / "projects" / project["source_path"] == source
            assert backend.all_jobs() == []
            assert backend.list_exports(directory.name) == []
            storage = backend.storage_for(directory.name, create=False)
            try:
                assert storage.load_chapter(0).segments[0].target == "一小段原文。"
            finally:
                storage.close()
        assert (directory / "state.sqlite3").read_bytes() == before
