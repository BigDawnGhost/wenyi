"""Small project and task helpers for local Desktop workflow tests."""

import time

import pytest
from wenyi_backend import dal
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.storage.sqlite import SqliteStorage


def wait_job(run_id, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = dal.get_job_by_arq_id(run_id)
        if job and job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"Local task did not finish within {timeout}s: {job}")


def initialize(backend):
    pid = dal.create_project("Offline", "en", "zh", {"template": "标准翻译"})
    source = backend.project_dir(pid) / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("A short paragraph.\n", encoding="utf-8")
    store = backend.storage_for(pid)
    assert isinstance(store, SqliteStorage)
    doc = Document(
        title="Offline",
        source_path=str(source),
        fmt="txt",
        source_lang="en",
        target_lang="zh",
        chapters=[
            Chapter(index=0, title="Chapter", segments=[Segment(index=0, source="A paragraph.")])
        ],
    )
    store.init_from_document(doc)
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "译文"
    store.save_chapter_with_status(chapter, "done")
    dal.set_project_source(
        pid,
        source_path=str(source),
        book_title="Offline",
        source_sha256=store.load_manifest()["source_sha256"],
        fmt="txt",
    )
    dal.set_project_status(pid, "done")
    return pid, store
