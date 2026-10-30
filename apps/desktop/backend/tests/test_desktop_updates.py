"""Update admission uses temporary catalogs and mock tasks, never real providers."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from wenyi_desktop.local_runtime import LocalRuntime
from wenyi_desktop.main import create_app, create_context
from wenyi_desktop.updates import UpdateAdmissionMiddleware, UpdateGuard


@pytest.fixture
def context(tmp_path):
    context = create_context(tmp_path / "updates", api_token="test-token")
    yield context
    context.repository.close()


def test_authenticated_private_endpoints_and_isolation(context, tmp_path):
    app = create_app(context=context)
    context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    other = create_context(tmp_path / "other", api_token="other-token")
    other_app = create_app(context=other)
    other.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
    try:
        # Lifespan intentionally not entered: no real workers are needed.
        client = TestClient(app)
        peer = TestClient(other_app)
        headers = {"Authorization": "Bearer test-token"}
        assert client.post("/desktop/updates/prepare").status_code == 401
        assert client.post("/desktop/updates/cancel").status_code == 401
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 204
        assert client.put("/config", headers=headers).status_code == 409
        assert client.put("/desktop/credentials/test", headers=headers).status_code == 409
        assert client.post("/projects/upload", headers=headers).status_code == 409
        assert client.put("/config").status_code == 401
        assert not other_app.state.update_guard.closed
        assert (
            peer.post(
                "/desktop/updates/prepare", headers={"Authorization": "Bearer other-token"}
            ).status_code
            == 204
        )
        assert client.post("/desktop/updates/cancel", headers=headers).status_code == 204
        assert client.post("/desktop/updates/cancel", headers=headers).status_code == 204
        assert client.put("/config", headers=headers).status_code != 409
        assert other_app.state.update_guard.closed
        assert "/desktop/updates/prepare" not in app.openapi()["paths"]
        context.telemetry.runtime = None
        assert client.post("/desktop/updates/prepare", headers=headers).status_code == 503
        assert client.post("/desktop/updates/cancel", headers=headers).status_code == 503
    finally:
        other.repository.close()


@pytest.mark.parametrize("kind", ["parse", "translation", "review", "export"])
@pytest.mark.parametrize("status", ["queued", "running"])
def test_all_project_jobs_block_prepare(context, kind, status):
    backend = context.repository
    for name in ("one", "two"):
        pid = backend.create_project(name, "en", "zh", {})
    job = backend.create_job(pid, kind, "mock-task", run_id="mock-run")
    backend.set_job_status(job, status)
    runtime = SimpleNamespace(accepting=True, idle=True)
    context.telemetry.runtime = runtime
    guard = UpdateGuard(context.telemetry)
    with pytest.raises(HTTPException) as error:
        asyncio.run(guard.prepare())
    assert error.value.status_code == 409
    assert not guard.closed
    assert runtime.accepting


@pytest.mark.parametrize("owned", ["_submitted", "_active", "_cancelling"])
def test_actual_runtime_ownership_blocks_even_terminal_catalog(context, owned):
    async def run():
        runtime = LocalRuntime(context.repository, context)
        runtime.accepting = True
        context.telemetry.runtime = runtime
        pid = context.repository.create_project("terminal", "en", "zh", {})
        job = context.repository.create_job(pid, "export", "mock", run_id="mock")
        context.repository.set_job_status(job, "done")
        task = asyncio.create_task(asyncio.sleep(60))
        if owned == "_active":
            runtime._active["mock"] = task
        else:
            getattr(runtime, owned).add(task if owned == "_cancelling" else "mock")
        guard = UpdateGuard(context.telemetry)
        try:
            with pytest.raises(HTTPException) as error:
                await guard.prepare()
            assert error.value.status_code == 409
            assert runtime.accepting and not task.done() and not guard.closed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_asgi_inflight_mutation_and_future_admission(context):
    async def run():
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        guard = UpdateGuard(context.telemetry)
        entered, release = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            entered.set()
            await release.wait()

        middleware = UpdateAdmissionMiddleware(app, guard=guard, token=None)
        scope = {"type": "http", "method": "PUT", "path": "/config", "headers": []}
        request = asyncio.create_task(middleware(scope, None, None))
        await entered.wait()
        with pytest.raises(HTTPException):
            await guard.prepare()
        assert not guard.closed and guard.inflight == 1
        release.set()
        await request
        assert guard.inflight == 0
        await guard.prepare()
        messages = []

        async def send(message):
            messages.append(message)

        await middleware(scope, None, send)
        assert messages[0]["status"] == 409
        await guard.cancel()
        await middleware(scope, None, None)
        assert not guard.closed

    asyncio.run(run())


def test_invalid_header_bytes_are_rejected_without_entering_a_mutation(context):
    async def run():
        guard = UpdateGuard(context.telemetry)
        sent = []

        async def app(scope, receive, send):
            pytest.fail("Invalid credentials must not reach the application")

        async def send(message):
            sent.append(message)

        middleware = UpdateAdmissionMiddleware(app, guard=guard, token="test-token")
        scope = {
            "type": "http",
            "method": "PUT",
            "path": "/config",
            "headers": [(b"authorization", b"Bearer \xff")],
        }
        await middleware(scope, None, send)
        assert sent[0]["status"] == 401
        assert guard.inflight == 0

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["error", "cancel", "success"])
def test_check_closes_gate_and_serializes_cancel(context, monkeypatch, failure):
    async def run():
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        guard = UpdateGuard(context.telemetry)
        entered, release = threading.Event(), threading.Event()

        def check():
            entered.set()
            release.wait(5)
            if failure == "error":
                raise ValueError("mock catalog error")
            return []

        monkeypatch.setattr(context.repository, "all_jobs", check)
        prepare = asyncio.create_task(guard.prepare())
        await asyncio.to_thread(entered.wait, 5)
        assert guard.closed
        messages = []

        async def send(message):
            messages.append(message)

        async def forbidden(scope, receive, send):
            pytest.fail("A mutation entered during the asynchronous idle check")

        middleware = UpdateAdmissionMiddleware(forbidden, guard=guard, token=None)
        await middleware(
            {"type": "http", "method": "POST", "path": "/projects/upload", "headers": []},
            None,
            send,
        )
        assert messages[0]["status"] == 409
        cancel = asyncio.create_task(guard.cancel())
        await asyncio.sleep(0)
        assert not cancel.done() and guard.closed
        if failure == "cancel":
            prepare.cancel()
        release.set()
        result = await asyncio.gather(prepare, return_exceptions=True)
        if failure == "error":
            assert isinstance(result[0], ValueError)
        elif failure == "cancel":
            assert isinstance(result[0], asyncio.CancelledError)
        await cancel
        assert not guard.closed and context.telemetry.runtime.accepting

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_check_reopens_without_cancel_request(context, monkeypatch, cancelled):
    async def run():
        context.telemetry.runtime = SimpleNamespace(accepting=True, idle=True)
        guard = UpdateGuard(context.telemetry)
        entered, release = threading.Event(), threading.Event()

        def check():
            entered.set()
            release.wait(5)
            raise ValueError("mock failure")

        monkeypatch.setattr(context.repository, "all_jobs", check)
        request = asyncio.create_task(guard.prepare())
        await asyncio.to_thread(entered.wait, 5)
        if cancelled:
            request.cancel()
        release.set()
        result = await asyncio.gather(request, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError if cancelled else ValueError)
        assert not guard.closed

    asyncio.run(run())
