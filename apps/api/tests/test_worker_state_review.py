"""Regression checks for worker cancellation and stale delivery state races."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import test_storage_pg_integration as storage_tests
from type_helpers import must
from wenyi_api import dal
from wenyi_api.workers import tasks
from wenyi_core.config import Config
from wenyi_core.llm.limits import RequestLimits, RequestStopped
from wenyi_core.llm.providers.fake import FakeClient

pg_pool = storage_tests.pg_pool
pg_storage = storage_tests.pg_storage


@pytest.fixture
def worker_state(pg_storage, pg_pool, monkeypatch):
    from wenyi_core.llm import factory

    monkeypatch.setattr(dal, "get_pool", lambda: pg_pool)
    monkeypatch.setattr(tasks, "init_pool", lambda _: pg_pool)
    monkeypatch.setattr(tasks, "_pipeline_storage", lambda *_: pg_storage)
    monkeypatch.setattr(tasks, "redis_progress_fn", lambda *_a, **_k: lambda *_: None)
    config = Config.from_dict(
        {"language": {"source": "en", "target": "zh"}, "llm": {"preset": "fake"}}
    )
    monkeypatch.setattr(tasks, "_build_config_for", lambda *_: config)
    monkeypatch.setattr(factory, "build_client", lambda _: FakeClient())
    monkeypatch.setattr(
        tasks,
        "settings",
        SimpleNamespace(psycopg_dsn="unused", redis_url="redis://127.0.0.1:56379/0"),
    )
    return pg_storage


def test_cancelled_requests_pause_without_recording_an_error(worker_state, monkeypatch):
    pid = worker_state.project_id
    job_id = dal.create_job(pid, "translation", "cancelled-run", run_id="cancelled-run")
    dal.set_project_status(pid, "translating")

    def cancelled(*_):
        limits = RequestLimits(Config.from_dict({"llm": {"preset": "fake"}}).llm)
        limits.cancel()
        limits.check()

    monkeypatch.setattr(tasks, "_book_operation", cancelled)
    tasks._execute("translation", pid, "cancelled-run", {})
    assert must(dal.get_project(pid))["status"] == "paused"
    assert must(dal.get_project(pid))["error"] is None
    assert must(dal.get_job(job_id))["status"] == "paused"
    assert must(dal.get_job(job_id))["error"] is None
    events = worker_state.list_events()
    pause = next(event for event in events if event["event"] == "task_paused")
    assert pause["kind"] == "translation"
    assert pause["run_id"] == "cancelled-run"
    assert "reason" not in pause
    assert not any(event["event"] == "pipeline_error" for event in events)


@pytest.mark.parametrize(
    "reason",
    [
        "Model request budget exhausted",
        "Model request deadline reached; resume with a new deadline",
    ],
)
def test_limit_stops_keep_actionable_diagnostics(worker_state, monkeypatch, reason):
    pid = worker_state.project_id
    job_id = dal.create_job(pid, "translation", "budget-run", run_id="budget-run")
    dal.set_project_status(pid, "translating")

    def exhausted(*_):
        raise RequestStopped(reason)

    monkeypatch.setattr(tasks, "_book_operation", exhausted)
    tasks._execute("translation", pid, "budget-run", {})
    assert must(dal.get_project(pid))["status"] == "paused"
    assert must(dal.get_project(pid))["error"] == reason
    assert must(dal.get_job(job_id))["error"] == reason
    pause = next(event for event in worker_state.list_events() if event["event"] == "task_paused")
    assert pause["reason"] == reason


def test_model_failures_still_record_an_error(worker_state, monkeypatch):
    pid = worker_state.project_id
    job_id = dal.create_job(pid, "translation", "failed-model", run_id="failed-model")
    dal.set_project_status(pid, "translating")

    def failed(*_):
        raise RuntimeError("Provider connection failed")

    monkeypatch.setattr(tasks, "_book_operation", failed)
    with pytest.raises(RuntimeError, match="Provider connection failed"):
        tasks._execute("translation", pid, "failed-model", {})
    assert must(dal.get_project(pid))["status"] == "error"
    assert must(dal.get_project(pid))["error"] == "Provider connection failed"
    assert must(dal.get_job(job_id))["error"] == "Provider connection failed"
    assert any(event["event"] == "pipeline_error" for event in worker_state.list_events())


def test_superseded_redis_delivery_cannot_execute_over_new_work(worker_state, monkeypatch):
    pid = worker_state.project_id
    old = dal.create_job(pid, "translation", "old-delivery", run_id="old-delivery")
    dal.set_job_status(old, "interrupted")
    new = dal.create_job(pid, "translation", "new-delivery", run_id="new-delivery")
    dal.set_project_status(pid, "translating")
    calls = []

    def operation(*_):
        calls.append("unexpected old workflow")
        return "done"

    monkeypatch.setattr(tasks, "_book_operation", operation)
    tasks._execute("translation", pid, "old-delivery", {})
    assert calls == []
    assert must(dal.get_job(new))["status"] == "queued"
    assert must(dal.get_project(pid))["status"] == "translating"


def test_async_error_fallback_does_not_overwrite_a_newer_task(worker_state, monkeypatch):
    pid = worker_state.project_id
    old = dal.create_job(pid, "translation", "old-failure", run_id="old-failure")
    dal.set_project_status(pid, "translating")
    latest = []

    def failed_thread(*_):
        # The thread has persisted its failure and released the project lock.
        dal.set_job_status(old, "error", error="old service failure")
        dal.set_project_status(pid, "error", error="old service failure")
        # A user retry can now acquire the project before the Future exception
        # reaches _run's fallback handler on the event loop.
        latest.append(dal.create_job(pid, "translation", "new-retry", run_id="new-retry"))
        dal.set_project_status(pid, "translating")
        raise RuntimeError("old service failure")

    monkeypatch.setattr(tasks, "_execute", failed_thread)
    with pytest.raises(RuntimeError, match="old service failure"):
        asyncio.run(tasks._run("translation", pid, "old-failure", {}))
    assert must(dal.get_job(latest[0]))["status"] == "queued"
    assert must(dal.get_project(pid))["status"] == "translating"


def _aged_export(storage, pool, run_id, status="running"):
    export_id = dal.create_export(storage.project_id, "txt", {})
    job_id = dal.create_job(
        storage.project_id,
        "export",
        run_id,
        run_id=run_id,
        params={"export_id": export_id},
        status=status,
    )
    with pool.connection() as conn:
        conn.execute("UPDATE jobs SET updated_at=now()-interval '3 minutes' WHERE id=%s", (job_id,))
    return export_id, job_id


def _export_row(pool, export_id):
    with pool.connection() as conn:
        return conn.execute("SELECT status,error FROM exports WHERE id=%s", (export_id,)).fetchone()


@pytest.fixture
def recovery_state(worker_state, pg_pool, monkeypatch):
    from wenyi_api.storage_pg import PostgresStorage
    from wenyi_api.workers import recovery

    monkeypatch.setattr(recovery, "get_pool", lambda: pg_pool)
    monkeypatch.setattr(
        recovery,
        "storage_for",
        lambda pid: PostgresStorage(pid, pg_pool, run_dir=worker_state.run_dir),
    )
    return worker_state, recovery


def test_export_recovery_marks_orphan_error_without_changing_project(recovery_state, pg_pool):
    storage, recovery = recovery_state
    dal.set_project_status(storage.project_id, "translating")
    export_id, job_id = _aged_export(storage, pg_pool, "orphan-render")
    asyncio.run(recovery.recover_jobs({"redis": None}))
    assert must(dal.get_job(job_id))["status"] == "error"
    assert _export_row(pg_pool, export_id)[0] == "error"
    assert must(dal.get_project(storage.project_id))["status"] == "translating"


def test_export_lock_excludes_recovery_but_not_other_exports_or_translation(
    recovery_state, pg_pool
):
    storage, recovery = recovery_state
    active_export, active_job = _aged_export(storage, pg_pool, "active-render")
    orphan_export, orphan_job = _aged_export(storage, pg_pool, "orphan-render")
    with storage.export_lock(active_export):
        with storage.lock(blocking=False):
            with storage.export_lock(orphan_export, blocking=False):
                pass
        asyncio.run(recovery.recover_jobs({"redis": None}))
    assert must(dal.get_job(active_job))["status"] == "running"
    assert _export_row(pg_pool, active_export)[0] == "pending"
    assert must(dal.get_job(orphan_job))["status"] == "error"
    assert _export_row(pg_pool, orphan_export)[0] == "error"


def test_export_recovery_uses_export_queue_and_retains_live_queued_jobs(
    recovery_state, pg_pool, monkeypatch
):
    from arq.jobs import JobStatus

    storage, recovery = recovery_state
    queued = {}
    for remote_status in (
        JobStatus.queued,
        JobStatus.deferred,
        JobStatus.in_progress,
        JobStatus.complete,
        JobStatus.not_found,
    ):
        run_id = "export-" + remote_status.value
        queued[run_id] = (*_aged_export(storage, pg_pool, run_id, status="queued"), remote_status)
    checks = []

    class RemoteJob:
        def __init__(self, job_id, redis, *, _queue_name):
            self.job_id = job_id
            checks.append(_queue_name)

        async def status(self):
            return queued[self.job_id][2]

    monkeypatch.setattr(recovery, "Job", RemoteJob)
    asyncio.run(recovery.recover_jobs({"redis": None}))
    assert checks == ["wenyi:exports"] * 5
    for export_id, job_id, remote_status in queued.values():
        expected = (
            "queued"
            if remote_status in {JobStatus.queued, JobStatus.deferred, JobStatus.in_progress}
            else "error"
        )
        assert must(dal.get_job(job_id))["status"] == expected
        assert _export_row(pg_pool, export_id)[0] == (
            "pending" if expected == "queued" else "error"
        )


def test_failure_does_not_leave_project_busy_when_api_briefly_owns_lock(worker_state, pg_pool):
    import threading

    from wenyi_api.storage_pg import PostgresStorage

    pid = worker_state.project_id
    job = dal.create_job(
        pid, "translation", "failed-between-locks", run_id="failed-between-locks", status="running"
    )
    dal.set_project_status(pid, "translating")
    observer = PostgresStorage(pid, pg_pool, run_dir=worker_state.run_dir)
    acquired = threading.Event()

    def api_request():
        with observer.lock():
            acquired.set()
            # A concurrent API guard briefly owns the lock while reading status.
            threading.Event().wait(0.15)

    thread = threading.Thread(target=api_request)
    thread.start()
    assert acquired.wait(2)
    tasks._record_terminal_status(pid, "failed-between-locks", RuntimeError("worker failed"))
    thread.join(2)
    assert must(dal.get_job(job))["status"] == "error"
    assert must(dal.get_project(pid))["status"] == "error"


def test_export_render_uses_enqueued_config_snapshot(pg_storage, pg_pool, monkeypatch, tmp_path):
    from pathlib import Path

    from wenyi_api.project_service import config_document
    from wenyi_core.assemble import writer

    storage_tests.initialize(pg_storage, tmp_path)
    pid = pg_storage.project_id
    monkeypatch.setattr(dal, "get_pool", lambda: pg_pool)
    monkeypatch.setattr(tasks, "init_pool", lambda _: pg_pool)
    monkeypatch.setattr(tasks, "_pipeline_storage", lambda *_: pg_storage)
    monkeypatch.setattr(tasks.paths, "project_dir", lambda _: pg_storage.run_dir)
    monkeypatch.setattr(tasks.paths, "exports_dir", lambda _: str(tmp_path / pid / "exports"))
    monkeypatch.setattr(
        tasks, "settings", SimpleNamespace(psycopg_dsn="unused", data_dir=str(tmp_path))
    )
    original_config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "output": {"punctuation_normalize": False},
            "pipeline": {"babeldoc_timeout": 123},
        }
    )
    export_id = dal.create_export(pid, "txt", {})
    job_id = dal.create_job(
        pid,
        "export",
        "snapshot-export",
        run_id="snapshot-export",
        params={"export_id": export_id},
        config_snapshot=config_document(original_config),
    )
    changed_config = config_document(original_config)
    changed_config["output"]["punctuation_normalize"] = True
    changed_config["pipeline"]["babeldoc_timeout"] = 999
    dal.set_project_config(pid, changed_config)
    rendered = []

    def assemble(store, source, **options):
        rendered.append(options)
        Path(options["out_path"]).write_text("rendered", encoding="utf-8")

    monkeypatch.setattr(writer, "assemble", assemble)
    tasks._export_sync(pid, export_id=export_id, run_id="snapshot-export", fmt="txt")
    assert rendered[0]["punctuation_normalize"] is False
    assert rendered[0]["babeldoc_timeout"] == 123
    assert must(dal.get_job(job_id))["status"] == "done"
    assert _export_row(pg_pool, export_id)[0] == "done"


def test_cancelled_export_waits_for_render_thread_to_publish(monkeypatch):
    import threading

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def render(*_a, **_kw):
        started.set()
        assert release.wait(3)
        finished.set()
        return 1

    monkeypatch.setattr(tasks, "_export_sync", render)

    async def run():
        job = asyncio.create_task(tasks.run_export({}, project_id="p", export_id=1))
        assert await asyncio.to_thread(started.wait, 2)
        job.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not job.done(), "Cancelled coroutine must retain its worker slot while render lives"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert finished.is_set()

    asyncio.run(run())


def test_pause_monitor_cancels_waiting_model_without_progress_callback(worker_state, monkeypatch):
    import threading

    from wenyi_core.llm import factory

    pid = worker_state.project_id
    job_id = dal.create_job(pid, "translation", "monitor-cancel", run_id="monitor-cancel")
    dal.set_project_status(pid, "translating")
    entered = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    errors = []
    limits = RequestLimits(Config.from_dict({"llm": {"preset": "fake"}}).llm)

    class WaitingClient(FakeClient):
        def cancel(self):
            limits.cancel()
            cancelled.set()

    client = WaitingClient()
    monkeypatch.setattr(factory, "build_client", lambda _: client)

    def wait_for_model(*_):
        # The request produces no progress callbacks while waiting on a permit or retry.
        entered.set()
        if not cancelled.wait(3):
            raise AssertionError("Pause monitor did not cancel the waiting client")
        limits.check()

    monkeypatch.setattr(tasks, "_book_operation", wait_for_model)

    def execute():
        try:
            tasks._execute("translation", pid, "monitor-cancel", {})
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert entered.wait(2)
        dal.set_project_status(pid, "pausing")
        # The watcher polls every 250 ms; allow scheduling/DB latency without
        # requiring an incidental progress callback to notice the request.
        assert cancelled.wait(2)
        assert finished.wait(2)
    finally:
        cancelled.set()
        thread.join(3)
    assert not errors
    assert not thread.is_alive()
    assert must(dal.get_project(pid))["status"] == "paused"
    assert must(dal.get_job(job_id))["status"] == "paused"
    assert must(dal.get_project(pid))["error"] is None
    assert must(dal.get_job(job_id))["error"] is None
