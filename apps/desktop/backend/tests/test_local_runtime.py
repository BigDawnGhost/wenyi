"""Local runtime ownership, bounded scheduling and durable restart contracts."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import anyio
import pytest
from desktop_test_support import initialize
from starlette.websockets import WebSocketDisconnect
from wenyi_backend import dal
from wenyi_backend.context import current_context, use_context
from wenyi_desktop import local_runtime
from wenyi_desktop.local_runtime import LocalRuntime, ProgressHub
from wenyi_desktop.main import create_context


@pytest.fixture
def catalog(tmp_path):
    context = create_context(tmp_path / "desktop")
    backend = context.repository
    try:
        with use_context(context):
            yield backend
    finally:
        backend.close()


def project():
    return dal.create_project("Runtime", "en", "zh", {})


def enqueue(runtime, pid, run, kind="translation"):
    identity = dal.create_job(pid, kind, run, run_id=run)
    runtime.enqueue("run_" + kind, _job_id=run, project_id=pid, run_id=run)
    return identity


def test_workspace_second_owner_is_rejected_and_release_allows_restart(catalog):
    async def run():
        first = LocalRuntime(catalog, current_context())
        second = LocalRuntime(catalog, current_context())
        await first.start()
        try:
            with pytest.raises(RuntimeError, match="already open"):
                await second.start()
            assert first.accepting
        finally:
            await asyncio.wait_for(first.stop(), 3)
        await second.start()
        await asyncio.wait_for(second.stop(), 3)

    asyncio.run(run())


@pytest.mark.parametrize("status", ["queued", "running"])
def test_recovery_pauses_orphan_workflows_and_fails_exports(catalog, status):
    async def run():
        pid, other = project(), project()
        job = dal.create_job(pid, "translation", "orphan", run_id="orphan")
        dal.set_job_status(job, status)
        dal.set_project_status(pid, "translating")
        eid, export_job = catalog.admit_export(other, "txt", {}, "orphan-export", {})
        dal.set_job_status(export_job, status)
        runtime = LocalRuntime(catalog, current_context())
        await runtime.start()
        try:
            assert dal.get_job(job)["status"] == "interrupted"
            assert dal.get_project(pid)["status"] == "paused"
            assert dal.get_job(export_job)["status"] == "error"
            assert catalog.list_exports(other)[0]["id"] == eid
            assert catalog.list_exports(other)[0]["status"] == "error"
        finally:
            await asyncio.wait_for(runtime.stop(), 3)

    asyncio.run(run())


def test_duplicate_delivery_and_independent_two_export_slots(catalog, monkeypatch):
    from wenyi_backend.workers import tasks

    async def run():
        runtime = LocalRuntime(catalog, current_context())
        release = asyncio.Event()
        workflow_started = asyncio.Event()
        exports_started = asyncio.Event()
        calls = []

        async def execute(ctx, *, project_id, run_id):
            calls.append(run_id)
            job = dal.get_job_by_arq_id(run_id)
            dal.set_job_status(job["id"], "running")
            if run_id == "workflow":
                workflow_started.set()
            if len([value for value in calls if value.startswith("export")]) == 2:
                exports_started.set()
            await release.wait()
            dal.set_job_status(job["id"], "done")

        monkeypatch.setattr(tasks, "run_translation", execute)
        monkeypatch.setattr(tasks, "run_export", execute)
        await runtime.start()
        try:
            pid = project()
            enqueue(runtime, pid, "workflow")
            runtime.enqueue(
                "run_translation", _job_id="workflow", project_id=pid, run_id="workflow"
            )
            await asyncio.wait_for(workflow_started.wait(), 3)
            enqueue(runtime, project(), "queued-workflow")
            for index in range(3):
                enqueue(runtime, project(), f"export-{index}", "export")
            await asyncio.wait_for(exports_started.wait(), 3)
            assert calls.count("workflow") == 1
            assert "queued-workflow" not in calls
            assert "export-2" not in calls
            release.set()
            await asyncio.wait_for(runtime.workflow_queue.join(), 3)
            await asyncio.wait_for(runtime.export_queue.join(), 3)
            runtime.enqueue(
                "run_translation", _job_id="workflow", project_id=pid, run_id="workflow"
            )
            assert calls.count("workflow") == 1
        finally:
            release.set()
            await asyncio.wait_for(runtime.stop(), 3)

    asyncio.run(run())


def test_queue_is_bounded_and_shutdown_interrupts_unstarted_work(catalog):
    async def run():
        runtime = LocalRuntime(catalog, current_context())
        # Without starting consumers, admission can be tested deterministically.
        runtime.accepting = True
        pid = project()
        for index in range(runtime.workflow_queue.maxsize):
            enqueue(runtime, pid, f"queued-{index}")
        with pytest.raises(asyncio.QueueFull):
            enqueue(runtime, project(), "overflow")
        assert "overflow" not in runtime._submitted
        assert runtime.export_queue.empty()
        await asyncio.wait_for(runtime.stop(), 10)
        assert all(
            row["status"] == "interrupted"
            for row in catalog.all_jobs()
            if row["run_id"] != "overflow"
        )
        assert dal.get_project(pid)["status"] == "paused"
        with pytest.raises(RuntimeError, match="not accepting"):
            runtime.enqueue("run_parse")

    asyncio.run(run())


def test_progress_cache_is_bounded_expires_and_persists_latest(catalog, monkeypatch):
    async def run():
        clock = [100.0]
        monkeypatch.setattr(local_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        hub = ProgressHub(catalog, asyncio.get_running_loop())
        pid = project()
        for index in range(257):
            hub.set(f"cache:{index}", str(index), ex=10)
        assert hub.get("cache:0") is None
        assert hub.get("cache:256") == "256"
        clock[0] += 11
        assert hub.get("cache:256") is None
        with hub.subscribe(pid) as queue:
            for index in range(65):
                hub._deliver(f"project:{pid}", str(index))
            assert queue.qsize() == 64
            assert queue.get_nowait() == "1"
            hub._deliver("project:unrelated", "not ours")
            assert queue.qsize() == 63
        latest = {
            "project_id": pid,
            "run_id": "run-latest",
            "done": 2,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        hub.set(f"project:{pid}:progress", json.dumps({**latest, "done": 1}), ex=60)
        hub.set(f"project:{pid}:progress", json.dumps(latest), ex=60)
        await hub.flush()
        assert catalog.load_progress(pid) == latest
        workspace = catalog.workspace
        catalog.close()
        reopened = create_context(workspace)
        try:
            assert local_runtime.progress_snapshot(reopened.repository, None, pid) == latest
        finally:
            reopened.repository.close()

    asyncio.run(run())


def test_websocket_first_frame_auth_snapshot_and_project_run_identity(desktop):
    client, backend, _ = desktop
    pid, store = initialize(backend)
    with client.websocket_connect(f"/ws/projects/{pid}/progress") as ws:
        ws.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008
    with client.websocket_connect(f"/ws/projects/{pid}/progress") as ws:
        ws.send_json({"token": "offline-test-token"})
        snapshot = ws.receive_json()
        assert snapshot["kind"] == "snapshot"
        assert snapshot["project"]["id"] == pid
        assert snapshot["chapters"][0]["index"] == 0
        payload = {"kind": "progress", "project_id": pid, "run_id": "visible-run", "done": 1}
        client.app.state.backend.telemetry.connect().publish(f"project:{pid}", json.dumps(payload))
        assert ws.receive_json() == payload
    store.close()


def test_progress_relay_cancellation_drains_children_and_unsubscribes(catalog, monkeypatch):
    from wenyi_desktop.main import DesktopServices

    async def run():
        pid = project()
        hub = ProgressHub(catalog, asyncio.get_running_loop())
        services = DesktopServices(catalog)
        services.runtime = SimpleNamespace(hub=hub)
        tasks = []
        drained = []
        create_task = asyncio.create_task

        def track(coro, **kwargs):
            async def child():
                try:
                    return await coro
                finally:
                    await asyncio.sleep(0)
                    drained.append(True)

            task = create_task(child(), **kwargs)
            tasks.append(task)
            return task

        monkeypatch.setattr(asyncio, "create_task", track)

        async def send_json(payload):
            assert payload["kind"] == "snapshot"

        with anyio.CancelScope() as scope:

            async def receive():
                # TestClient closes its owning AnyIO scope while relay children run.
                scope.cancel()
                await asyncio.Future()

            await services.relay(SimpleNamespace(send_json=send_json, receive=receive), pid)
        assert tasks and all(task.done() for task in tasks)
        assert len(drained) == len(tasks)
        assert hub._subscribers == {}

    anyio.run(run)


def test_workflow_cached_progress_rejects_other_project_and_old_run(desktop):
    client, backend, _ = desktop
    pid, store = initialize(backend)
    job = dal.create_job(pid, "translation", "latest-run", run_id="latest-run")
    dal.set_job_status(job, "done")
    payload = {
        "kind": "translation",
        "project_id": pid,
        "run_id": "latest-run",
        "done": 1,
        "total": 2,
        "label": "Saved",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    services = client.app.state.backend.telemetry
    hub = services.connect()
    key = f"project:{pid}:progress"
    url = f"/projects/{pid}/workflow"
    for invalid in ({**payload, "project_id": "other"}, {**payload, "run_id": "older"}):
        hub.set(key, json.dumps(invalid), ex=60)
        assert client.get(url).json()["progress"] is None
    hub.set(key, json.dumps(payload), ex=60)
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["progress"]["run_id"] == "latest-run"
    client.portal.call(hub.flush)
    assert backend.load_progress(pid) == payload
    client.portal.call(services.runtime.stop)

    async def restart():
        services.runtime = LocalRuntime(backend, client.app.state.backend)
        await services.runtime.start()

    client.portal.call(restart)
    try:
        assert services.connect().get(key) is None
        assert client.get(url).json()["progress"]["run_id"] == "latest-run"
    finally:
        client.portal.call(services.runtime.stop)
    store.close()
