"""Each Web app and worker owns its pool and stops its own background work."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

from fastapi.testclient import TestClient
from wenyi_api import adapters, main, workers
from wenyi_api.config import Settings
from wenyi_backend.context import BackendContext, current_context


class FakePool:
    def __init__(self, dsn):
        self.dsn = dsn
        self.close_calls = 0
        self.queries = []

    @contextmanager
    def connection(self):
        assert self.close_calls == 0
        yield self

    def execute(self, query):
        assert self.close_calls == 0
        self.queries.append(query)

    def close(self):
        self.close_calls += 1


def test_web_lifespans_start_and_close_only_their_own_pool(monkeypatch):
    pools = []

    def initialize(dsn):
        pool = FakePool(dsn)
        pools.append(pool)
        return pool

    monkeypatch.setattr(adapters, "init_pool", initialize)
    a = main.create_app(Settings(database_url="postgresql://fixture/a", api_token=None))
    b = main.create_app(Settings(database_url="postgresql://fixture/b", api_token=None))
    with TestClient(b) as client_b:
        pool_b = b.state.backend.repository.pool
        with TestClient(a) as client_a:
            pool_a = a.state.backend.repository.pool
            assert pool_a is not pool_b
            assert pool_a.dsn.endswith("/a") and pool_b.dsn.endswith("/b")
            assert client_a.get("/health").json()["db"] == "ok"
            assert client_b.get("/health").json()["db"] == "ok"
        assert pool_a.close_calls == 1
        assert pool_b.close_calls == 0
        assert client_b.get("/health").json()["db"] == "ok"
    assert len(pools) == 2
    assert all(pool.close_calls == 1 for pool in pools)
    assert pool_a.queries == ["SELECT 1"]
    assert pool_b.queries == ["SELECT 1", "SELECT 1"]


def test_worker_shutdown_stops_recovery_before_closing_its_pool(monkeypatch):
    pools, stopped = [], []

    def initialize(dsn):
        pool = FakePool(dsn)
        pools.append(pool)
        return pool

    async def recover(ctx):
        assert current_context() is ctx["backend"]
        pool = ctx["backend"].repository.pool
        ctx["entered"].set()
        try:
            await asyncio.Event().wait()
        finally:
            assert pool.close_calls == 0
            stopped.append(pool)

    monkeypatch.setattr(adapters, "init_pool", initialize)
    monkeypatch.setattr(workers, "recover_jobs", recover)

    async def exercise():
        entered_a, entered_b = asyncio.Event(), asyncio.Event()
        a: dict[str, object] = {"entered": entered_a}
        b: dict[str, object] = {"entered": entered_b}
        try:
            await workers.startup(a)
            await workers.startup(b)
            await asyncio.wait_for(asyncio.gather(entered_a.wait(), entered_b.wait()), 5)
            context_a, context_b = a["backend"], b["backend"]
            assert isinstance(context_a, BackendContext)
            assert isinstance(context_b, BackendContext)
            pool_a = adapters.postgres_repository(context_a).pool
            pool_b = adapters.postgres_repository(context_b).pool
            assert isinstance(pool_a, FakePool)
            assert isinstance(pool_b, FakePool)
            assert pool_a is not pool_b
            await workers.shutdown(a)
            assert stopped == [pool_a]
            assert pool_a.close_calls == 1 and pool_b.close_calls == 0
            recovery_task = b["recovery_task"]
            assert isinstance(recovery_task, asyncio.Task)
            assert not recovery_task.done()
            await workers.shutdown(b)
            assert stopped == [pool_a, pool_b]
            assert pool_b.close_calls == 1
        finally:
            for ctx in (a, b):
                if "recovery_task" in ctx:
                    await workers.shutdown(ctx)

    asyncio.run(exercise())
    assert len(pools) == 2
    assert all(pool.close_calls == 1 for pool in pools)
