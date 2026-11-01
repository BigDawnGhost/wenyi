"""Real catalog and artifact transactions preserve pause and source publication invariants."""

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from wenyi_api import dal
from wenyi_api.storage_pg import PostgresStorage
from wenyi_backend.context import current_context
from wenyi_backend.routers import projects
from wenyi_backend.routers.dependencies import storage_for


@pytest.mark.parametrize("status", ["queued", "running", "done", "error", "paused", "interrupted"])
def test_pause_requires_an_active_matching_job(api, status):
    pid = dal.create_project("Pause identity", "en", "zh", {})
    dal.create_job(pid, "translation", pid, run_id=pid, status=status)
    dal.set_project_status(pid, "translating")
    active = status in {"queued", "running"}
    assert dal.request_pause(pid, pid) is active
    assert dal.get_project(pid)["status"] == ("pausing" if active else "translating")


def test_pause_rechecks_completion_and_new_run(api, monkeypatch):
    pid = dal.create_project("Pause race", "en", "zh", {})
    old_run, new_run = pid + "-old", pid + "-new"
    job = dal.create_job(pid, "translation", old_run, run_id=old_run, status="running")
    dal.set_project_status(pid, "translating")
    require_project = projects.require_project

    def finish_after_read(identity):
        project = require_project(identity)
        dal.set_project_status(identity, "done")
        dal.set_job_status(job, "done")
        return project

    monkeypatch.setattr(projects, "require_project", finish_after_read)
    with pytest.raises(HTTPException) as raised:
        projects.pause(pid)
    assert raised.value.status_code == 409
    assert dal.get_project(pid)["status"] == "done"
    dal.create_job(pid, "translation", new_run, run_id=new_run)
    dal.create_job(pid, "export", pid + "-export", run_id=pid + "-export")
    dal.set_project_status(pid, "translating")
    assert not dal.request_pause(pid, old_run)
    assert dal.get_project(pid)["status"] == "translating"
    assert dal.request_pause(pid, new_run)
    assert dal.request_pause(pid, new_run)


@pytest.fixture
def uploaded_project(api):
    context = current_context()
    pid = dal.create_project("Source ownership", "en", "zh", {})
    source = Path(context.project_dir(pid)) / "original.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Original source.", encoding="utf-8")
    dal.set_project_source(pid, str(source), "Original", fmt="text")
    storage = storage_for(pid)
    for key in ("preview.json", "parsed_document.json", "subtitle_preview.json"):
        storage.write_artifact(key, {"original": key})
    storage.close()
    return pid, source


def test_cache_invalidation_failure_rolls_back_before_source_publication(
    uploaded_project, monkeypatch
):
    pid, source = uploaded_project
    before = dal.get_project(pid)
    delete_artifact = PostgresStorage.delete_artifact

    def fail_after_first_delete(storage, key):
        if key == "parsed_document.json":
            raise OSError("Cache invalidation failed")
        return delete_artifact(storage, key)

    monkeypatch.setattr(PostgresStorage, "delete_artifact", fail_after_first_delete)
    with pytest.raises(OSError, match="Cache invalidation failed"):
        asyncio.run(
            projects.upload_source(
                pid, UploadFile(BytesIO(b"Replacement."), filename="new.txt"), None
            )
        )
    assert dal.get_project(pid) == before
    assert source.is_file()
    assert not list(source.parent.glob("source-*"))
    storage = storage_for(pid)
    try:
        for key in ("preview.json", "parsed_document.json", "subtitle_preview.json"):
            assert storage.read_artifact(key) == {"original": key}
    finally:
        storage.close()


def test_lost_publication_acknowledgement_keeps_the_new_original(uploaded_project, monkeypatch):
    pid, old_source = uploaded_project
    publish = dal.set_project_source

    def publish_then_fail(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("Acknowledgement lost")

    monkeypatch.setattr(current_context().repository, "set_project_source", publish_then_fail)
    with pytest.raises(OSError, match="Acknowledgement lost"):
        asyncio.run(
            projects.upload_source(
                pid, UploadFile(BytesIO(b"Replacement."), filename="new.txt"), None
            )
        )
    referenced = Path(current_context().data_dir) / dal.get_project(pid)["source_path"]
    assert referenced.read_bytes() == b"Replacement."
    assert old_source.is_file()
