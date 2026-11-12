"""Permanent Web deletion cleans only owned resources and preserves retry on failure."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from api_test_support import initialize, new_project
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_api import adapters, dal
from wenyi_api.adapters import create_context, postgres_repository
from wenyi_api.config import Settings
from wenyi_api.main import create_app
from wenyi_backend.context import current_context


@pytest.fixture
def deletion_api(tmp_path, monkeypatch):
    settings = replace(Settings(), data_dir=str(tmp_path / "data"), api_token=None)
    context = create_context(settings)
    repository = postgres_repository(context)
    records = {"book": {"id": "book", "status": "uploaded"}}
    connections = []
    busy = {"value": False}

    class Connection:
        in_transaction = False

        def execute(self, sql, parameters=()):
            self.in_transaction = True
            if "pg_try_advisory_lock" in sql:
                return SimpleNamespace(fetchone=lambda: (True,))
            if "SELECT EXISTS" in sql:
                return SimpleNamespace(fetchone=lambda: (busy["value"],))
            return SimpleNamespace(fetchone=lambda: None)

        def commit(self):
            self.in_transaction = False

        rollback = commit

    @contextmanager
    def connection():
        value = Connection()
        connections.append(value)
        try:
            yield value
        finally:
            value.commit()

    monkeypatch.setattr(repository, "_pool", SimpleNamespace(connection=connection))
    monkeypatch.setattr(repository, "get_project", records.get)
    monkeypatch.setattr(dal, "delete_project", lambda pid, **_: records.pop(pid))
    monkeypatch.setattr(
        repository,
        "storage_for",
        lambda pid: SimpleNamespace(lock=lambda **_: nullcontext(), close=lambda: None),
    )
    directory = Path(settings.data_dir) / "book"
    directory.mkdir(parents=True)
    (directory / "source.txt").write_text("Original.", encoding="utf-8")
    (directory / "exports").mkdir()
    (directory / "exports" / "translation.txt").write_text("Translation.", encoding="utf-8")
    client = TestClient(create_app(context=context))
    try:
        yield client, directory, records, busy, connections
    finally:
        client.close()


def test_web_deletion_removes_files_before_catalog_and_keeps_other_projects(deletion_api):
    client, directory, records, _, _ = deletion_api
    other = directory.with_name("other")
    other.mkdir()
    source = other / "source.txt"
    source.write_text("Other project.", encoding="utf-8")
    response = client.delete("/projects/book")
    assert response.status_code == 200
    assert "book" not in records
    assert not directory.exists()
    assert source.read_text(encoding="utf-8") == "Other project."


def test_web_cleanup_failure_keeps_catalog_and_allows_retry(deletion_api, monkeypatch):
    client, directory, records, _, connections = deletion_api
    original = adapters.shutil.rmtree

    def denied(path):
        assert not any(conn.in_transaction for conn in connections)
        raise PermissionError("File in use")

    with monkeypatch.context() as patch:
        patch.setattr(adapters.shutil, "rmtree", denied)
        response = client.delete("/projects/book")
        assert response.status_code == 409
        assert "book" in records and directory.exists()
    assert adapters.shutil.rmtree is original
    assert client.delete("/projects/book").status_code == 200


def test_web_active_export_or_job_blocks_deletion(deletion_api):
    client, directory, records, busy, _ = deletion_api
    busy["value"] = True
    assert client.delete("/projects/book").status_code == 409
    assert "book" in records and directory.exists()


def test_web_missing_resource_directory_still_deletes_catalog(deletion_api):
    client, directory, records, _, _ = deletion_api
    import shutil

    shutil.rmtree(directory)
    assert client.delete("/projects/book").status_code == 200
    assert "book" not in records


def test_web_deletion_rejects_redirected_project_directory(deletion_api, tmp_path):
    client, directory, records, _, _ = deletion_api
    protected = tmp_path / "protected"
    directory.rename(protected)
    try:
        directory.symlink_to(protected, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable")
    assert client.delete("/projects/book").status_code == 409
    assert "book" in records
    assert (protected / "source.txt").read_text(encoding="utf-8") == "Original."


def test_postgres_deletion_cascades_state_and_removes_owned_resources(api):
    client, _ = api
    pid = new_project(api)
    repository = postgres_repository(current_context())
    directory = Path(repository.project_dir(pid))
    directory.mkdir(parents=True)
    storage = repository.storage_for(pid)
    initialize(storage, directory)
    storage.write_artifact("test.json", {"saved": True})
    dal.create_job(pid, "translation", "finished-job", status="done")
    export_id = dal.create_export(pid, "txt", {})
    output = directory / "exports" / "translation.txt"
    output.parent.mkdir()
    output.write_text("Saved translation.", encoding="utf-8")
    current_context().exports.publish_export(pid, export_id, str(output))
    response = client.delete(f"/projects/{pid}")
    assert response.status_code == 200, response.text
    assert dal.get_project(pid) is None and not directory.exists()
    with repository.pool.connection() as connection:
        for table in ("chapters", "segments", "artifacts", "jobs", "exports"):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE project_id=%s", (pid,)
            ).fetchone() == (0,)


@pytest.mark.parametrize(
    "kind,status",
    [("job", "queued"), ("job", "running"), ("export", "pending"), ("export", "running")],
)
def test_postgres_active_tasks_prevent_file_and_record_deletion(api, kind, status):
    client, _ = api
    pid = new_project(api)
    repository = postgres_repository(current_context())
    directory = Path(repository.project_dir(pid))
    directory.mkdir(parents=True)
    source = directory / "source.txt"
    source.write_text("Original.", encoding="utf-8")
    if kind == "job":
        dal.create_job(pid, "translation", "active-job", status=status)
    else:
        export_id = dal.create_export(pid, "txt", {})
        dal.set_export_status(export_id, status)
    assert client.delete(f"/projects/{pid}").status_code == 409
    assert dal.get_project(pid) is not None and source.exists()


def test_postgres_cleanup_failure_preserves_translation_and_retries(api, monkeypatch):
    client, _ = api
    pid = new_project(api)
    repository = postgres_repository(current_context())
    directory = Path(repository.project_dir(pid))
    directory.mkdir(parents=True)
    storage = repository.storage_for(pid)
    initialize(storage, directory)

    def denied(path):
        raise PermissionError("File in use")

    with monkeypatch.context() as patch:
        patch.setattr(adapters.shutil, "rmtree", denied)
        assert client.delete(f"/projects/{pid}").status_code == 409
        assert storage.load_chapter(0).segments[0].target == "润色译文"
    assert client.delete(f"/projects/{pid}").status_code == 200


def test_postgres_deletion_serializes_export_admission_without_open_transaction(api, monkeypatch):
    client, _ = api
    pid = new_project(api)
    context = current_context()
    repository = postgres_repository(context)
    directory = Path(repository.project_dir(pid))
    directory.mkdir(parents=True)
    entered, release, requesting = Event(), Event(), Event()
    original = adapters.shutil.rmtree

    def remove(path):
        entered.set()
        assert release.wait(10)
        original(path)

    def export():
        requesting.set()
        return context.exports.admit_export(pid, "txt", {}, "late-export", {})

    monkeypatch.setattr(adapters.shutil, "rmtree", remove)
    with ThreadPoolExecutor(max_workers=2) as executor:
        deletion = executor.submit(client.delete, f"/projects/{pid}")
        try:
            assert entered.wait(10)
            admission = executor.submit(copy_context().run, export)
            assert requesting.wait(10)
            assert not admission.done()
        finally:
            release.set()
        assert deletion.result(10).status_code == 200
        with pytest.raises(HTTPException) as error:
            admission.result(10)
        assert error.value.status_code == 404
    assert dal.get_project(pid) is None and not directory.exists()
