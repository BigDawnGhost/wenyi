"""Queue submission pool reuse (audit C-6 / worklist W-7)."""

from __future__ import annotations

import asyncio

import wenyi_api.queue as queue_module
from wenyi_backend.workers.signing import SIGNATURE_FIELD, payload_from, verify_task


class FakePool:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []
        self.calls: list[dict] = []

    async def enqueue_job(self, name, _queue_name, **kwargs):
        self.jobs.append((name, _queue_name))
        self.calls.append(kwargs)
        return f"job:{name}"


def _install_fake_pools(monkeypatch) -> list[object]:
    created: list[object] = []

    async def fake_create_pool(settings):
        pool = FakePool()
        created.append(settings)
        return pool

    monkeypatch.setattr(queue_module, "create_pool", fake_create_pool)
    monkeypatch.setattr(queue_module, "_pools", {})
    return created


def test_enqueue_reuses_pool_per_redis_url(monkeypatch):
    created = _install_fake_pools(monkeypatch)

    first = asyncio.run(queue_module.enqueue("redis://one", "run_parse", None, project_id="p"))
    second = asyncio.run(queue_module.enqueue("redis://one", "run_review", None, project_id="p"))

    assert (first, second) == ("job:run_parse", "job:run_review")
    assert len(created) == 1  # one pool for two submissions through the same URL
    assert queue_module._pools and len(queue_module._pools) == 1


def test_enqueue_keeps_pools_separate_per_url(monkeypatch):
    created = _install_fake_pools(monkeypatch)

    asyncio.run(queue_module.enqueue("redis://one", "run_export", None))
    asyncio.run(queue_module.enqueue("redis://two", "run_export", None))

    assert len(created) == 2
    assert len(queue_module._pools) == 2


def test_export_jobs_route_to_export_queue(monkeypatch):
    _install_fake_pools(monkeypatch)

    asyncio.run(queue_module.enqueue("redis://one", "run_export", None))
    asyncio.run(queue_module.enqueue("redis://one", "run_parse", None))

    pool = next(iter(queue_module._pools.values()))
    assert isinstance(pool, FakePool)
    assert [queue_name for _name, queue_name in pool.jobs] == [
        queue_module.EXPORT_QUEUE,
        queue_module.WORKFLOW_QUEUE,
    ]


def test_enqueue_signs_task_payload(monkeypatch):
    _install_fake_pools(monkeypatch)

    asyncio.run(
        queue_module.enqueue("redis://one", "run_parse", "secret", project_id="p", run_id="r")
    )

    pool = next(iter(queue_module._pools.values()))
    assert isinstance(pool, FakePool)
    kwargs = pool.calls[0]
    assert verify_task("run_parse", payload_from(kwargs), kwargs[SIGNATURE_FIELD], "secret")


def test_enqueue_replaces_caller_supplied_signature(monkeypatch):
    _install_fake_pools(monkeypatch)

    asyncio.run(
        queue_module.enqueue(
            "redis://one", "run_parse", "secret", project_id="p", payload_signature="fake"
        )
    )

    pool = next(iter(queue_module._pools.values()))
    assert isinstance(pool, FakePool)
    kwargs = pool.calls[0]
    assert kwargs[SIGNATURE_FIELD] != "fake"
    assert verify_task("run_parse", payload_from(kwargs), kwargs[SIGNATURE_FIELD], "secret")
