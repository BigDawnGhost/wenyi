"""Workers preserve startup failures and cooperate with Arq cancellation."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from wenyi_api.job_service import start_job
from wenyi_api.workers import ExportWorkerSettings, WorkerSettings, tasks


@pytest.mark.parametrize(
    "kind",
    ["parse", "prepare", "translation", "chapter_translation", "review", "srt"],
)
def test_startup_failure_is_persisted(monkeypatch, kind):
    statuses = []
    monkeypatch.setattr(
        tasks.dal, "set_project_status", lambda pid, state, **kw: statuses.append((state, kw))
    )

    def fail(*args):
        raise RuntimeError("config missing")

    monkeypatch.setattr(tasks, "_execute", fail)
    with pytest.raises(RuntimeError, match="config missing"):
        asyncio.run(tasks._run(kind, "p", None, {}))
    assert statuses == [("error", {"error": "config missing"})]


def test_arq_cancellation_waits_for_state_to_be_flushed(monkeypatch):
    started, flushed = threading.Event(), threading.Event()

    def execute(kind, pid, run_id, params, stop):
        started.set()
        assert stop.wait(3)
        flushed.set()

    monkeypatch.setattr(tasks, "_execute", execute)

    async def run():
        job = asyncio.create_task(tasks._run("translation", "p", None, {}))
        await asyncio.to_thread(started.wait, 3)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert flushed.is_set()

    asyncio.run(run())


def test_exports_have_independent_queue():
    assert WorkerSettings.queue_name != ExportWorkerSettings.queue_name
    assert tasks.run_export not in WorkerSettings.functions
    assert tasks.run_export in ExportWorkerSettings.functions
    assert all("qa" not in fn.__name__ for fn in WorkerSettings.functions)


def test_model_comparison_cannot_be_enqueued():
    with pytest.raises(HTTPException) as raised:
        asyncio.run(start_job("test", "model_compare"))
    assert raised.value.status_code == 422
    assert all(fn.__name__ != "run_model_compare" for fn in WorkerSettings.functions)


def test_resolve_source_never_guesses_using_title(monkeypatch, tmp_path):
    source = tmp_path / "source.html"
    source.write_text("<p>Text</p>", encoding="utf-8")
    monkeypatch.setattr(
        tasks.dal, "get_project", lambda pid: {"title": None, "source_path": "source.html"}
    )
    monkeypatch.setattr(tasks, "settings", SimpleNamespace(data_dir=str(tmp_path)))
    assert tasks._resolve_source("p") == str(source)


def test_export_cancellation_waits_for_render_to_finish(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def render(*args, **kwargs):
        started.set()
        assert release.wait(3)
        finished.set()
        return 1

    monkeypatch.setattr(tasks, "_export_sync", render)

    async def run():
        task = asyncio.create_task(tasks.run_export({}, project_id="p", export_id=1, fmt="txt"))
        await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(run())
