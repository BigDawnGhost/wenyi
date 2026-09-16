"""Real PostgreSQL request lifecycle and independent-model publication contracts."""

import json

import pytest
import test_storage_pg_integration as storage_tests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.fake_llm import MeteredFakeClient, routing_handler
from wenyi_api import dal, project_service, retranslation_service
from wenyi_api.db import pool as db_pool
from wenyi_api.routers import configuration, projects, retranslation
from wenyi_api.workers import retranslation as worker
from wenyi_core.config import Config
from wenyi_core.llm import factory

pg_pool = storage_tests.pg_pool
pg_storage = storage_tests.pg_storage


@pytest.fixture
def interactive(pg_pool, pg_storage, monkeypatch, tmp_path):
    storage_tests.initialize(pg_storage, tmp_path)
    monkeypatch.setattr(db_pool, "_pool", pg_pool)
    for module in (project_service, retranslation_service, worker):
        monkeypatch.setattr(module, "storage_for", lambda pid: pg_storage)
    monkeypatch.setattr(
        retranslation_service,
        "effective_config",
        lambda project: Config.from_dict(
            {
                "language": {"source": "en", "target": "zh"},
                "llm": {"preset": "fake"},
                "pipeline": {"polish": True},
            }
        ),
    )
    monkeypatch.setattr(
        factory, "build_client", lambda config: MeteredFakeClient(handler=routing_handler)
    )
    monkeypatch.setattr(worker, "init_pool", lambda dsn: pg_pool)
    queue = []

    async def enqueue(name, **params):
        queue.append((name, params))
        return object()

    monkeypatch.setattr(retranslation, "enqueue", enqueue)
    app = FastAPI()
    app.include_router(retranslation.router)
    app.include_router(projects.router)
    app.include_router(configuration.router)
    with TestClient(app) as client:
        yield client, pg_storage, queue


def submit(client, storage):
    return client.post(
        f"/projects/{storage.project_id}/chapters/0/retranslate", json={"segment_indices": [0]}
    )


def test_retranslate_with_active_workflow_keeps_status_and_rejects_duplicate(interactive):
    client, storage, queue = interactive
    dal.set_project_status(storage.project_id, "translating")
    stale_chapter = storage.load_chapter(0)
    with storage.lock():
        response = submit(client, storage)
        assert response.status_code == 202, response.text
        assert submit(client, storage).status_code == 409
        rid = response.json()["id"]
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            executor.submit(worker.execute_retranslation, storage.project_id, rid).result(timeout=5)
        finally:
            executor.shutdown(wait=False)
        assert dal.get_project(storage.project_id)["status"] == "translating"
    state = retranslation_service.get_request(storage.project_id, rid)
    assert state["status"] == "done", state["error"]
    assert state["applied"] == [0]
    assert state["usage"]["totals"]["calls"] > 0
    assert storage.load_chapter(0).segments[0].target == "润0"
    storage.save_chapter(stale_chapter)
    assert storage.load_chapter(0).segments[0].target == "润0"
    assert queue[0][0] == "run_retranslation"
    # Redelivery must not make another request or overwrite later text.
    worker.execute_retranslation(storage.project_id, rid)
    assert retranslation_service.get_request(storage.project_id, rid)["usage"] == state["usage"]
    stats = client.get(f"/projects/{storage.project_id}/stats").json()
    assert stats["usage"]["totals"]["calls"] == state["usage"]["totals"]["calls"]


def test_change_during_model_call_preserves_newer_text(interactive, monkeypatch):
    client, storage, _ = interactive
    response = submit(client, storage)
    rid = response.json()["id"]

    def changed(messages, tier, json_mode):
        chapter = storage.load_chapter(0)
        chapter.segments[0].target = "更新的人工译文"
        storage.save_chapter(chapter)
        return routing_handler(messages, tier, json_mode)

    monkeypatch.setattr(factory, "build_client", lambda config: MeteredFakeClient(handler=changed))
    worker.execute_retranslation(storage.project_id, rid)
    state = retranslation_service.get_request(storage.project_id, rid)
    assert state["status"] == "conflict", state["error"]
    assert state["conflicts"] == [0]
    assert storage.load_chapter(0).segments[0].target == "更新的人工译文"


def test_latest_glossary_used_after_request_submission(interactive, monkeypatch):
    from wenyi_core.glossary.store import GlossaryTerm

    client, storage, _ = interactive
    rid = submit(client, storage).json()["id"]
    storage.upsert_term(GlossaryTerm(source="Original", target="更新术语"))
    prompts = []

    def captured(messages, tier, json_mode):
        prompts.append(json.dumps(messages, ensure_ascii=False))
        return routing_handler(messages, tier, json_mode)

    monkeypatch.setattr(factory, "build_client", lambda config: MeteredFakeClient(handler=captured))
    worker.execute_retranslation(storage.project_id, rid)
    assert any("更新术语" in text for text in prompts)
    assert retranslation_service.get_request(storage.project_id, rid)["status"] == "done"


def test_request_validation_queue_failure_and_delete_guard(interactive, monkeypatch):
    client, storage, _ = interactive
    url = f"/projects/{storage.project_id}/chapters/0/retranslate"
    for indices in ([], [0, 0], [-1], [True], list(range(51))):
        assert client.post(url, json={"segment_indices": indices}).status_code == 422
    assert client.post(url, json={"segment_indices": [100]}).status_code == 404

    async def offline(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(retranslation, "enqueue", offline)
    assert submit(client, storage).status_code == 503
    assert retranslation_service.requests_for(storage.project_id)[0]["status"] == "error"
    request = retranslation_service.create_request(storage.project_id, 0, [0])
    assert client.delete(f"/projects/{storage.project_id}").status_code == 409
    retranslation_service.set_result(storage.project_id, request["id"], "error")
    assert client.delete(f"/projects/{storage.project_id}").status_code == 200


def test_untranslated_segment_cannot_be_retranslated(interactive):
    client, storage, _ = interactive
    chapter = storage.load_chapter(0)
    chapter.segments[0].target = None
    storage.save_chapter(chapter)
    assert submit(client, storage).status_code == 409


def test_real_redis_export_queue_dispatches_paragraph_worker(interactive, monkeypatch):
    import asyncio
    import os
    from dataclasses import replace

    from arq import create_pool
    from arq.connections import RedisSettings
    from arq.worker import Worker
    from wenyi_api import workers

    dsn = os.environ.get("WENYI_TEST_REDIS_URL")
    if not dsn:
        pytest.skip("Set WENYI_TEST_REDIS_URL to an isolated Redis test database")
    client, storage, _ = interactive
    monkeypatch.setattr(workers, "settings", replace(workers.settings, redis_url=dsn))
    monkeypatch.setattr(retranslation, "enqueue", workers.enqueue)
    response = submit(client, storage)
    assert response.status_code == 202, response.text
    rid = response.json()["id"]

    async def consume():
        pool = await create_pool(RedisSettings.from_dsn(dsn))
        consumer = Worker(
            [worker.run_retranslation],
            redis_pool=pool,
            queue_name=workers.EXPORT_QUEUE,
            burst=True,
            handle_signals=False,
            max_jobs=1,
            job_timeout=30,
        )
        try:
            await consumer.async_run()
            assert consumer.jobs_complete == 1
        finally:
            await consumer.close()
            await pool.delete("arq:result:" + rid)
            await pool.aclose()

    asyncio.run(consume())
    result = retranslation_service.get_request(storage.project_id, rid)
    assert result["status"] == "done", result["error"]
    assert result["applied"] == [0]


def test_recovery_marks_orphan_request_failed_without_pausing_book(interactive):
    import asyncio

    client, storage, _ = interactive
    dal.set_project_status(storage.project_id, "translating")
    rid = submit(client, storage).json()["id"]
    retranslation_service.set_result(storage.project_id, rid, "running", applied=[0])
    with storage._conn as conn:
        conn.execute(
            "UPDATE retranslation_requests SET updated_at=now()-interval '3 minutes' WHERE id=%s",
            (rid,),
        )
    asyncio.run(worker.recover_retranslations({}))
    request = retranslation_service.get_request(storage.project_id, rid)
    assert request["status"] == "error"
    assert request["applied"] == [0]
    assert dal.get_project(storage.project_id)["status"] == "translating"
