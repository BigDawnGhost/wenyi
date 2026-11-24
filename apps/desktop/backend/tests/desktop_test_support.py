"""Small project and task helpers for local Desktop workflow tests."""

import time

import pytest
from wenyi_backend.context import BackendContext, current_context
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.sqlite import SqliteStorage
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.main import DesktopServices
from wenyi_desktop.settings import DesktopSettings


def wait_job(run_id: str, *, timeout: float = 60):
    """Allow slow CI disks to commit real checkpoints without fixed test sleeps."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = current_context().repository.get_job_by_arq_id(run_id)
        if job and job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"Local task did not finish within {timeout}s: {job}")


def initialize(backend):
    pid = backend.create_project("Offline", "en", "zh", {"template": "标准翻译"})
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
    assert chapter is not None
    chapter.segments[0].target = "译文"
    store.save_chapter_with_status(chapter, "done")
    manifest = store.load_manifest()
    assert manifest is not None
    backend.set_project_source(
        pid,
        source_path=str(source),
        book_title="Offline",
        source_sha256=manifest["source_sha256"],
        fmt="txt",
    )
    backend.set_project_status(pid, "done")
    return pid, store


def local_backend(context: BackendContext) -> LocalBackend:
    """Narrow a generic context to the Desktop repository implementation."""
    repository = context.repository
    assert isinstance(repository, LocalBackend)
    return repository


def desktop_settings(context: BackendContext) -> DesktopSettings:
    """Narrow a generic context to the Desktop settings implementation."""
    settings = context.settings_store
    assert isinstance(settings, DesktopSettings)
    return settings


def desktop_services(context: BackendContext) -> DesktopServices:
    """Narrow a generic context to the Desktop telemetry implementation."""
    services = context.telemetry
    assert isinstance(services, DesktopServices)
    return services
