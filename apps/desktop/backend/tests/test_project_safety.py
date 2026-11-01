"""Project commands preserve source ownership and reject stale pause requests."""

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from wenyi_backend.routers import projects
from wenyi_core.storage.sqlite import SqliteStorage


def test_pause_cannot_overwrite_completion_after_read(desktop_context, monkeypatch):
    backend = desktop_context.repository
    pid = backend.create_project("Pause race", "en", "zh", {})
    job_id = backend.create_job(pid, "translation", "run", run_id="run", status="running")
    backend.set_project_status(pid, "translating")
    require_project = projects.require_project

    def finish_after_read(identity):
        project = require_project(identity)
        backend.set_project_status(identity, "done")
        backend.set_job_status(job_id, "done")
        return project

    monkeypatch.setattr(projects, "require_project", finish_after_read)
    with pytest.raises(HTTPException) as raised:
        projects.pause(pid)
    assert raised.value.status_code == 409
    assert backend.get_project(pid)["status"] == "done"
    assert backend.get_job(job_id)["status"] == "done"


@pytest.mark.parametrize("status", ["queued", "running", "done", "error", "paused", "interrupted"])
def test_pause_requires_an_active_matching_job(desktop_context, status):
    backend = desktop_context.repository
    pid = backend.create_project("Pause identity", "en", "zh", {})
    backend.create_job(pid, "translation", "run", run_id="run", status=status)
    backend.set_project_status(pid, "translating")
    active = status in {"queued", "running"}
    assert backend.request_pause(pid, "run") is active
    assert backend.get_project(pid)["status"] == ("pausing" if active else "translating")


def test_pause_cannot_cancel_a_newer_run(desktop_context):
    backend = desktop_context.repository
    pid = backend.create_project("New run", "en", "zh", {})
    backend.create_job(pid, "translation", "old", run_id="old", status="done")
    backend.create_job(pid, "translation", "new", run_id="new", status="queued")
    backend.create_job(pid, "export", "export", run_id="export", status="running")
    backend.set_project_status(pid, "translating")
    assert not backend.request_pause(pid, "old")
    assert backend.get_project(pid)["status"] == "translating"
    assert backend.request_pause(pid, "new")
    assert backend.request_pause(pid, "new"), "Repeated pause for the same active run is harmless"


@pytest.fixture
def uploaded_project(desktop_context):
    backend = desktop_context.repository
    pid = backend.create_project("Replace source", "en", "zh", {})
    source = backend.project_dir(pid) / "original.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Original source.", encoding="utf-8")
    backend.set_project_source(pid, str(source), "Original", fmt="text")
    storage = backend.storage_for(pid)
    for key in ("preview.json", "parsed_document.json", "subtitle_preview.json"):
        storage.write_artifact(key, {"original": key})
    storage.close()
    return backend, pid, source


def test_replacement_cache_failure_preserves_source_and_preview(uploaded_project, monkeypatch):
    backend, pid, source = uploaded_project
    before = backend.get_project(pid)
    delete_artifact = SqliteStorage.delete_artifact

    def fail_after_first_delete(storage, key):
        if key == "parsed_document.json":
            raise OSError("Cache invalidation failed")
        return delete_artifact(storage, key)

    monkeypatch.setattr(SqliteStorage, "delete_artifact", fail_after_first_delete)
    with pytest.raises(OSError, match="Cache invalidation failed"):
        asyncio.run(
            projects.upload_source(
                pid, UploadFile(BytesIO(b"Replacement source."), filename="new.txt"), None
            )
        )
    assert backend.get_project(pid) == before
    assert source.read_text(encoding="utf-8") == "Original source."
    assert not list(source.parent.glob("source-*"))
    storage = backend.storage_for(pid)
    try:
        for key in ("preview.json", "parsed_document.json", "subtitle_preview.json"):
            assert storage.read_artifact(key) == {"original": key}
    finally:
        storage.close()


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_source_publication_never_deletes_referenced_file(
    uploaded_project, desktop_context, monkeypatch, committed
):
    backend, pid, original = uploaded_project
    publish = backend.set_project_source

    def fail_publication(*args, **kwargs):
        if committed:
            publish(*args, **kwargs)
        raise OSError("Publication acknowledgement lost")

    monkeypatch.setattr(backend, "set_project_source", fail_publication)
    with pytest.raises(OSError, match="Publication acknowledgement lost"):
        asyncio.run(
            projects.upload_source(
                pid, UploadFile(BytesIO(b"Replacement source."), filename="new.txt"), None
            )
        )
    referenced = Path(desktop_context.data_dir) / backend.get_project(pid)["source_path"]
    assert referenced.is_file()
    assert referenced.read_bytes() == (b"Replacement source." if committed else b"Original source.")
    assert original.is_file()
