"""Application scopes survive HTTP, WebSocket, async workers, and thread dispatch."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from wenyi_backend.context import ContextMiddleware, current_context, use_context
from wenyi_backend.workers import tasks


def scoped_app(context):
    app = FastAPI()
    app.add_middleware(ContextMiddleware, context=context)

    @app.get("/sync")
    def sync():
        return {"owner": current_context().data_dir}

    @app.get("/thread")
    async def thread():
        return {"owner": await asyncio.to_thread(lambda: current_context().data_dir)}

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        await socket.accept()
        await socket.send_json(
            {"owner": await asyncio.to_thread(lambda: current_context().data_dir)}
        )
        await socket.close()

    return app


def test_concurrent_app_requests_and_websockets_never_select_another_app(backend_context):
    contexts = [replace(backend_context, data_dir=f"app-{index}") for index in range(2)]

    def exercise(context):
        with TestClient(scoped_app(context)) as client:
            for _ in range(10):
                assert client.get("/sync").json() == {"owner": context.data_dir}
                assert client.get("/thread").json() == {"owner": context.data_dir}
                with client.websocket_connect("/ws") as socket:
                    assert socket.receive_json() == {"owner": context.data_dir}

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(exercise, contexts))
    assert current_context() is backend_context


def test_worker_owns_its_context_not_the_submitters_scope(backend_context, monkeypatch):
    seen = []
    contexts = [replace(backend_context, data_dir=f"worker-{index}") for index in range(2)]

    def execute(kind, pid, run, params, stop):
        seen.append((pid, current_context().data_dir))

    monkeypatch.setattr(tasks, "_execute", execute)

    async def run():
        await asyncio.gather(
            *(
                tasks.run_parse({"backend": context}, project_id=context.data_dir)
                for context in contexts
            )
        )
        assert current_context() is backend_context

    asyncio.run(run())
    assert sorted(seen) == [(context.data_dir, context.data_dir) for context in contexts]


def test_nested_scope_restores_parent_on_error(backend_context):
    other = replace(backend_context, repository=SimpleNamespace())
    with pytest.raises(ValueError):
        with use_context(other):
            assert current_context() is other
            raise ValueError("interrupted")
    assert current_context() is backend_context
