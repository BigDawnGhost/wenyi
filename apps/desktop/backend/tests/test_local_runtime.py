"""Independent local workflows, bounded exports and durable restart contracts."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import anyio
import pytest
from desktop_test_support import initialize
from starlette.websockets import WebSocketDisconnect
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
    return current_context().repository.create_project("Runtime", "en", "zh", {})


def enqueue(runtime, pid, run, kind="translation"):
    identity = current_context().repository.create_job(pid, kind, run, run_id=run)
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


@pytest.mark.parametrize("count", [3, 8])
def test_workflows_overlap_without_occupying_the_default_executor(catalog, monkeypatch, count):
    from wenyi_backend.execution import runner

    async def run():
        context = current_context()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        runtime = LocalRuntime(catalog, context)
        started = {f"parallel-{index}": asyncio.Event() for index in range(count)}
        release = threading.Event()
        owners = {}

        def execute(request, dependencies, stop):
            pid, run_id = request.project_id, request.run_id
            owners[run_id] = (current_context(), threading.get_ident(), pid)
            job = dependencies.jobs.get_job_by_arq_id(run_id)
            dependencies.jobs.set_job_status(job["id"], "running")
            loop.call_soon_threadsafe(started[run_id].set)
            assert release.wait(10), "The test did not release its synchronous workflows"
            dependencies.jobs.set_job_status(job["id"], "done")

        monkeypatch.setattr(runner, "execute", execute)
        await runtime.start()
        try:
            for run_id in started:
                enqueue(runtime, project(), run_id)
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 3)
            assert all(owner is context for owner, _, _ in owners.values())
            assert len({thread for _, thread, _ in owners.values()}) == count
            assert len({pid for _, _, pid in owners.values()}) == count
            assert not runtime.idle
            assert (
                await asyncio.wait_for(asyncio.to_thread(lambda: "responsive"), 1) == "responsive"
            )
        finally:
            release.set()
            await asyncio.wait_for(runtime.stop(), 5)
        assert runtime.idle
        assert all(job["status"] == "done" for job in catalog.all_jobs())

    asyncio.run(run())


def test_diagnostics_distinguish_runner_timeout_from_user_cancellation(catalog, monkeypatch):
    import io

    from wenyi_backend.workers import tasks
    from wenyi_desktop.diagnostics import Reporter

    stream = io.StringIO()
    reporter = Reporter(stream)

    async def execute(ctx, **values):
        await asyncio.Future()

    monkeypatch.setattr(tasks, "run_translation", execute)

    async def run():
        runtime = LocalRuntime(catalog, current_context(), diagnostics=reporter)
        runtime.accepting = True
        queue = asyncio.Queue()
        await queue.put(
            ("run_translation", {"project_id": "fake-private-project", "run_id": "run"}, "run")
        )
        await queue.put(None)
        await asyncio.wait_for(runtime._consume(queue, 0.01), 3)
        assert runtime.idle

    try:
        asyncio.run(run())
    finally:
        reporter.close()
    rows = [
        json.loads(line.removeprefix("WENYI_DIAGNOSTIC "))
        for line in stream.getvalue().splitlines()
    ]
    assert [row["event"] for row in rows] == ["task_started", "task_timeout", "task_cancelled"]
    assert all(row["attributes"]["kind"] == "translation" for row in rows)
    assert len({row["attributes"]["run_ref"] for row in rows}) == 1
    assert "fake-private-project" not in stream.getvalue()


@pytest.mark.parametrize("status", ["queued", "running"])
def test_recovery_pauses_orphan_workflows_and_fails_exports(catalog, status):
    async def run():
        pid, other = project(), project()
        job = current_context().repository.create_job(pid, "translation", "orphan", run_id="orphan")
        current_context().repository.set_job_status(job, status)
        current_context().repository.set_project_status(pid, "translating")
        eid, export_job = catalog.admit_export(other, "txt", {}, "orphan-export", {})
        current_context().repository.set_job_status(export_job, status)
        runtime = LocalRuntime(catalog, current_context())
        await runtime.start()
        try:
            assert current_context().repository.get_job(job)["status"] == "interrupted"
            assert current_context().repository.get_project(pid)["status"] == "paused"
            assert current_context().repository.get_job(export_job)["status"] == "error"
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
        other_workflow_started = asyncio.Event()
        exports_started = asyncio.Event()
        calls = []

        async def execute(ctx, *, project_id, run_id):
            calls.append(run_id)
            job = current_context().repository.get_job_by_arq_id(run_id)
            current_context().repository.set_job_status(job["id"], "running")
            if run_id == "workflow":
                workflow_started.set()
            if run_id == "other-workflow":
                other_workflow_started.set()
            if len([value for value in calls if value.startswith("export")]) == 2:
                exports_started.set()
            await release.wait()
            current_context().repository.set_job_status(job["id"], "done")

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
            enqueue(runtime, project(), "other-workflow")
            for index in range(3):
                enqueue(runtime, project(), f"export-{index}", "export")
            await asyncio.wait_for(other_workflow_started.wait(), 3)
            await asyncio.wait_for(exports_started.wait(), 3)
            assert calls.count("workflow") == 1
            assert "other-workflow" in calls
            assert "export-2" not in calls
            release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(runtime._workflows)), 3)
            await asyncio.wait_for(runtime.export_queue.join(), 3)
            runtime.enqueue(
                "run_translation", _job_id="workflow", project_id=pid, run_id="workflow"
            )
            assert calls.count("workflow") == 1
        finally:
            release.set()
            await asyncio.wait_for(runtime.stop(), 3)

    asyncio.run(run())


def test_export_queue_remains_bounded_and_shutdown_fails_unstarted_exports(catalog):
    async def run():
        runtime = LocalRuntime(catalog, current_context())
        assert runtime.export_queue.maxsize == 128
        # Exercise overflow and durable draining, not a 128-job disk benchmark.
        # No consumers are started, so admission remains deterministic.
        runtime.export_queue = asyncio.Queue(maxsize=2)
        runtime.accepting = True
        pid = project()
        for index in range(runtime.export_queue.maxsize):
            run_id = f"export-{index}"
            eid, _ = catalog.admit_export(pid, "txt", {}, run_id, {})
            runtime.enqueue(
                "run_export", _job_id=run_id, project_id=pid, run_id=run_id, export_id=eid
            )
        eid, _ = catalog.admit_export(pid, "txt", {}, "overflow", {})
        with pytest.raises(asyncio.QueueFull):
            runtime.enqueue(
                "run_export", _job_id="overflow", project_id=pid, run_id="overflow", export_id=eid
            )
        assert "overflow" not in runtime._submitted
        await asyncio.wait_for(runtime.stop(), 10)
        assert runtime.idle
        assert all(
            row["status"] == "error" for row in catalog.all_jobs() if row["run_id"] != "overflow"
        )
        with pytest.raises(RuntimeError, match="not accepting"):
            runtime.enqueue("run_parse")

    asyncio.run(run())


@pytest.mark.parametrize("supervisor_started", [False, True])
def test_shutdown_owns_every_workflow_before_worker_entry(catalog, monkeypatch, supervisor_started):
    from wenyi_backend.workers import tasks

    async def never_execute(*args, **kwargs):
        pytest.fail("Shutdown must not enter an unstarted domain workflow")

    monkeypatch.setattr(tasks, "run_translation", never_execute)

    async def run():
        runtime = LocalRuntime(catalog, current_context())
        runtime.accepting = True
        # Exceed the former admission queue size without allocating model or worker threads.
        for index in range(129):
            pid = project()
            catalog.set_project_status(pid, "translating")
            enqueue(runtime, pid, f"unstarted-{index}")
        assert not runtime.idle
        if supervisor_started:
            # Supervisors create their children; stop before those children enter the adapter.
            await asyncio.sleep(0)
            assert len(runtime._active) == 129
        await runtime.stop()
        assert runtime.idle
        assert all(row["status"] == "interrupted" for row in catalog.all_jobs())
        assert all(row["status"] == "paused" for row in catalog.list_projects())

    asyncio.run(run())


def test_thread_allocation_failure_terminates_only_its_workflow(catalog, monkeypatch):
    from wenyi_backend.execution import runner

    allocations = []

    def executor(**kwargs):
        allocations.append(kwargs)
        if len(allocations) == 1:
            raise RuntimeError("No workflow thread available")
        return ThreadPoolExecutor(**kwargs)

    def execute(request, dependencies, stop):
        job = dependencies.jobs.get_job_by_arq_id(request.run_id)
        dependencies.jobs.set_job_status(job["id"], "done")

    monkeypatch.setattr(local_runtime, "ThreadPoolExecutor", executor)
    monkeypatch.setattr(runner, "execute", execute)

    async def run():
        runtime = LocalRuntime(catalog, current_context())
        await runtime.start()
        try:
            failed = project()
            enqueue(runtime, failed, "failed")
            enqueue(runtime, project(), "healthy")
            await asyncio.wait_for(asyncio.gather(*tuple(runtime._workflows)), 3)
            assert runtime.idle
            assert catalog.get_project(failed)["status"] == "error"
            assert catalog.get_job_by_arq_id("failed")["error"] == "No workflow thread available"
            assert catalog.get_job_by_arq_id("healthy")["status"] == "done"
        finally:
            await asyncio.wait_for(runtime.stop(), 3)

    asyncio.run(run())


def test_shutdown_waits_for_all_parallel_writers(catalog, monkeypatch):
    from wenyi_backend.execution import runner

    async def run():
        loop = asyncio.get_running_loop()
        runtime = LocalRuntime(catalog, current_context())
        started = [asyncio.Event() for _ in range(3)]
        stopping = [asyncio.Event() for _ in range(3)]
        finished = [asyncio.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]

        def execute(request, dependencies, stop):
            run_id = request.run_id
            index = int(run_id)
            job = dependencies.jobs.get_job_by_arq_id(run_id)
            dependencies.jobs.set_job_status(job["id"], "running")
            loop.call_soon_threadsafe(started[index].set)
            assert stop.wait(5)
            loop.call_soon_threadsafe(stopping[index].set)
            assert release[index].wait(5)
            dependencies.jobs.set_job_status(job["id"], "paused")
            loop.call_soon_threadsafe(finished[index].set)

        monkeypatch.setattr(runner, "execute", execute)
        await runtime.start()
        shutdown = None
        try:
            for index in range(3):
                enqueue(runtime, project(), str(index))
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 3)
            shutdown = asyncio.create_task(runtime.stop())
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in stopping)), 3)
            release[0].set()
            await asyncio.wait_for(finished[0].wait(), 3)
            assert not shutdown.done()
            assert not runtime.idle
            assert not runtime.accepting
        finally:
            for event in release:
                event.set()
            if shutdown is not None:
                await asyncio.wait_for(shutdown, 5)
            else:
                await runtime.stop()
        assert all(event.is_set() for event in finished)
        assert runtime.idle
        assert all(job["status"] == "paused" for job in catalog.all_jobs())

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
    job = current_context().repository.create_job(
        pid, "translation", "latest-run", run_id="latest-run"
    )
    current_context().repository.set_job_status(job, "done")
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
