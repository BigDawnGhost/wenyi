"""Real PostgreSQL API/worker contracts, with deterministic offline model responses."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_storage_pg_integration import pg_pool  # noqa: F401
from type_helpers import must
from wenyi_api import dal, job_service
from wenyi_api.db import pool as pool_module
from wenyi_api.main import create_app
from wenyi_api.project_service import storage_for
from wenyi_api.routers import export
from wenyi_api.workers import tasks

from tests.fake_llm import MeteredFakeClient, routing_handler


@pytest.fixture
def api(monkeypatch, pg_pool, tmp_path):  # noqa: F811
    from wenyi_api.config import settings
    from wenyi_core.llm import factory

    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  preset: fake\n", encoding="utf-8")
    overrides = replace(
        settings,
        data_dir=str(tmp_path / "data"),
        config_path=str(config),
        api_token=None,
        redis_url="redis://127.0.0.1:56379/0",
    )
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith("wenyi_api") and hasattr(module, "settings"):
            monkeypatch.setattr(module, "settings", overrides)
    monkeypatch.setattr(pool_module, "_pool", pg_pool)
    monkeypatch.setattr(tasks, "init_pool", lambda dsn: pg_pool)
    monkeypatch.setattr(
        factory, "build_client", lambda cfg: MeteredFakeClient(handler=routing_handler)
    )
    queue = []

    async def enqueue(name, **kwargs):
        queue.append((name, kwargs))
        return SimpleNamespace(job_id=kwargs["_job_id"])

    monkeypatch.setattr(job_service, "enqueue", enqueue)
    monkeypatch.setattr(export, "enqueue", enqueue)
    client = TestClient(create_app())
    yield client, queue
    client.close()


def new_project(api, source="en", target="zh"):
    client, _ = api
    response = client.post(
        "/projects", json={"name": "Book", "source_lang": source, "target_lang": target}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def execute_next(api):
    _, queue = api
    name, params = queue.pop(0)
    params.pop("_job_id")
    asyncio.run(getattr(tasks, name)({}, **params))


def upload(api, pid, filename="book.html", data=None):
    client, _ = api
    data = (
        data
        or b"<html><body><h1>Chapter One</h1><p>The book begins.</p><h1>Chapter Two</h1><p>The story continues.</p></body></html>"
    )
    response = client.post(f"/projects/{pid}/upload", files={"file": (filename, data)})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "parse"
    assert client.get(f"/projects/{pid}/preview").status_code == 409
    execute_next(api)
    assert client.get(f"/projects/{pid}/preview").status_code == 200


def test_full_web_book_workflow_and_independent_export(api):
    client, _ = api
    pid = new_project(api)
    upload(api, pid)
    config = client.get(f"/projects/{pid}/config").json()
    assert config["effective"]["pipeline"]["review_autofix"]
    assert client.post(f"/projects/{pid}/prepare").status_code == 200
    assert client.put(f"/projects/{pid}/config", json={"yaml": config["yaml"]}).status_code == 409
    execute_next(api)
    assert client.get(f"/projects/{pid}").json()["status"] == "prepared"
    assert client.post(f"/projects/{pid}/translate").status_code == 200
    execute_next(api)
    project = client.get(f"/projects/{pid}").json()
    assert project["status"] == "done" and project["done_chapters"] == 2
    reviews = client.get(f"/projects/{pid}/review/runs").json()
    assert reviews and reviews[0]["status"] == "completed"
    assert client.get(f"/projects/{pid}/stats").json()["usage"]["totals"]["calls"] > 0
    # Export remains available while a workflow owns the project's write status.
    dal.set_project_status(pid, "translating")
    response = client.post(f"/projects/{pid}/exports", json={"format": "docx", "bilingual": True})
    assert response.status_code == 200, response.text
    execute_next(api)
    assert must(dal.get_project(pid))["status"] == "translating"
    eid = response.json()["export_id"]
    download = client.get(f"/projects/{pid}/exports/{eid}/download")
    assert download.status_code == 200 and download.content.startswith(b"PK")
    assert client.post(f"/projects/{pid}/qa").status_code == 404
    other = new_project(api)
    assert client.get(f"/projects/{other}/exports/{eid}/download").status_code == 404


@pytest.mark.parametrize(
    "yaml",
    [
        "language: []",
        "pipeline: null",
        "llm: {routes: null}",
        "llm: {models: []}",
        "pipeline: {consistency_qa: true}",
        "paths: {state_dir: /tmp/x}",
    ],
)
def test_config_errors_return_422(api, yaml):
    client, _ = api
    pid = new_project(api)
    response = client.post(f"/projects/{pid}/config/validate", json={"yaml": yaml})
    assert response.status_code == 422, response.text


def test_queue_failure_and_retry_preserve_original_task_kind(api, monkeypatch):
    client, queue = api
    pid = new_project(api)
    upload(api, pid)
    actual_enqueue = job_service.enqueue

    async def fail(*args, **kwargs):
        raise ConnectionError("redis offline")

    monkeypatch.setattr(job_service, "enqueue", fail)
    assert client.post(f"/projects/{pid}/prepare").status_code == 503
    assert must(dal.get_project(pid))["status"] == "uploaded"
    monkeypatch.setattr(job_service, "enqueue", actual_enqueue)
    response = client.post(f"/projects/{pid}/resume")
    assert response.status_code == 200 and response.json()["kind"] == "prepare"
    assert "config_snapshot" not in queue[0][1]
    execute_next(api)
    assert must(dal.get_project(pid))["status"] == "prepared"


def test_pause_and_resume_parse_preserves_job_identity(api):
    client, _ = api
    pid = new_project(api)
    response = client.post(
        f"/projects/{pid}/upload", files={"file": ("book.txt", b"A little book.")}
    )
    run_id = response.json()["job_id"]
    assert client.post(f"/projects/{pid}/pause").status_code == 200
    execute_next(api)
    assert must(dal.get_project(pid))["status"] == "paused"
    assert must(dal.get_job_by_arq_id(run_id))["status"] == "paused"
    response = client.post(f"/projects/{pid}/resume")
    assert response.status_code == 200 and response.json()["kind"] == "parse"
    execute_next(api)
    assert must(dal.get_project(pid))["status"] == "uploaded"


def test_source_identity_and_config_snapshot(api):
    client, _ = api
    pid = new_project(api)
    upload(api, pid)
    response = client.post(f"/projects/{pid}/prepare")
    run_id = response.json()["job_id"]
    dal.set_project_config(pid, {"llm": {"preset": "deepseek"}})
    assert tasks._build_config_for(pid, run_id).llm.preset == "fake"
    execute_next(api)
    assert storage_for(pid).exists()
    assert (
        client.post(
            f"/projects/{pid}/upload", files={"file": ("replacement.txt", b"Replacement")}
        ).status_code
        == 409
    )


def test_srt_full_workflow_manual_edit_resume_and_exports(api, monkeypatch):
    import json

    from wenyi_core.llm import factory

    client, _ = api
    pid = new_project(api)
    source = (
        b"1\n00:00:00,100 --> 00:00:01,200\nHello\n\n2\n00:00:01,300 --> 00:00:02,400\nGoodbye\n"
    )
    upload(api, pid, "video.srt", source)
    assert (
        client.put(
            f"/projects/{pid}/config", json={"yaml": "output: {mono: true, bilingual: true}"}
        ).status_code
        == 200
    )
    monkeypatch.setattr(
        factory,
        "build_client",
        lambda cfg: MeteredFakeClient(
            handler=lambda *_: json.dumps({"1": "你好", "2": "再见"}, ensure_ascii=False)
        ),
    )
    assert client.post(f"/projects/{pid}/translate").json()["kind"] == "srt"
    execute_next(api)
    assert client.get(f"/projects/{pid}").json()["initialized"]
    subtitles = client.get(f"/projects/{pid}/subtitles").json()
    assert subtitles["completed"] == 2
    assert client.get(f"/projects/{pid}/stats").json()["usage"]["totals"]["calls"] > 0
    assert len(client.get(f"/projects/{pid}/exports").json()) == 2
    assert (
        client.put(f"/projects/{pid}/subtitles/1", json={"target": "人工修订"}).status_code == 200
    )
    assert client.post(f"/projects/{pid}/translate").status_code == 200
    execute_next(api)
    assert client.get(f"/projects/{pid}/subtitles").json()["cues"][0]["target"] == "人工修订"
    response = client.post(f"/projects/{pid}/exports", json={"bilingual": True})
    assert response.status_code == 200
    execute_next(api)
    text = client.get(f"/projects/{pid}/exports/{response.json()['export_id']}/download").text
    assert "人工修订" in text and "00:00:00,100 --> 00:00:01,200" in text
    assert client.post(f"/projects/{pid}/review/run").status_code == 422


def test_live_redis_queue_executes_persisted_parse_job(api, monkeypatch):
    import os
    import uuid

    from arq import create_pool
    from arq.connections import RedisSettings
    from arq.worker import Worker
    from wenyi_api import workers

    redis_url = os.environ.get("WENYI_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set WENYI_TEST_REDIS_URL to run a real Arq queue")
    monkeypatch.setattr(workers, "settings", replace(workers.settings, redis_url=redis_url))
    queue_name = "wenyi:test:" + uuid.uuid4().hex
    monkeypatch.setattr(workers, "WORKFLOW_QUEUE", queue_name)
    monkeypatch.setattr(job_service, "enqueue", workers.enqueue)
    client, _ = api
    pid = new_project(api)
    response = client.post(
        f"/projects/{pid}/upload", files={"file": ("story.txt", b"A new story.")}
    )
    assert response.status_code == 200, response.text

    async def consume():
        redis = await create_pool(RedisSettings.from_dsn(redis_url))
        worker = Worker(
            functions=[tasks.run_parse],
            redis_pool=redis,
            queue_name=queue_name,
            burst=True,
            handle_signals=False,
            poll_delay=0.01,
        )
        try:
            await worker.async_run()
            assert worker.jobs_complete == 1 and worker.jobs_failed == 0
        finally:
            await worker.close()

    asyncio.run(consume())
    assert must(dal.get_job_by_arq_id(response.json()["job_id"]))["status"] == "done"
    assert client.get(f"/projects/{pid}/preview").status_code == 200


def test_dead_worker_status_can_resume_without_waiting_for_redis_ttl(api):
    from wenyi_api.workers.recovery import recover_jobs

    client, _ = api
    pid = new_project(api)
    upload(api, pid)
    response = client.post(f"/projects/{pid}/prepare")
    job = must(dal.get_job_by_arq_id(response.json()["job_id"]))
    dal.set_job_status(job["id"], "running")
    with pool_module.get_pool().connection() as conn:
        conn.execute(
            "UPDATE jobs SET updated_at=now()-interval '3 minutes' WHERE id=%s", (job["id"],)
        )
    # No live thread owns the project's advisory lock, even though DB says running.
    asyncio.run(recover_jobs({"redis": None}))
    assert must(dal.get_project(pid))["status"] == "paused"
    assert must(dal.latest_resumable_job(pid))["kind"] == "prepare"


def test_comparison_results_and_usage_survive_book_initialization(api, monkeypatch):
    from wenyi_core.llm import factory
    from wenyi_core.llm.router import RoutedLLMClient
    from wenyi_core.llm.usage import UsageSample

    class ComparisonClient(RoutedLLMClient):
        def complete_profile(self, messages, *, operation, profile, json_mode=False):
            self.usage.record(
                "direct",
                UsageSample(prompt_tokens=2, completion_tokens=3, total_tokens=5),
                operation,
            )
            return "comparison answer"

    client, _ = api
    pid = new_project(api)
    monkeypatch.setattr(factory, "build_client", lambda cfg: ComparisonClient(cfg.llm))
    response = client.post(
        f"/projects/{pid}/models/compare",
        json={
            "operation": "translation.body",
            "models": ["default_strong", "default_cheap"],
            "messages": [{"role": "user", "content": "test"}],
        },
    )
    assert response.status_code == 200, response.text
    execute_next(api)
    endpoint = f"/projects/{pid}/models/comparisons/{response.json()['job_id']}"
    assert client.get(endpoint).json()["status"] == "completed"
    assert client.get(f"/projects/{pid}/stats").json()["usage"]["totals"]["calls"] == 2
    upload(api, pid)
    monkeypatch.setattr(
        factory, "build_client", lambda cfg: MeteredFakeClient(handler=routing_handler)
    )
    assert client.post(f"/projects/{pid}/prepare").status_code == 200
    execute_next(api)
    stats = client.get(f"/projects/{pid}/stats").json()
    assert stats["usage"]["totals"]["calls"] >= 2
    assert any(run["operation"] == "model_compare" for run in stats["timing"]["runs"])
    assert client.get(endpoint).json()["status"] == "completed"


def test_http_download_and_websocket_require_token(api, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    from wenyi_api import main
    from wenyi_api.routers import ws

    client, _ = api
    pid = new_project(api)
    secured = replace(main.settings, api_token="test-access-token")
    monkeypatch.setattr(main, "settings", secured)
    monkeypatch.setattr(ws, "settings", secured)
    secured_client = TestClient(create_app())
    assert secured_client.get(f"/projects/{pid}/exports/123/download").status_code == 401
    assert (
        secured_client.get(
            f"/projects/{pid}", headers={"Authorization": "Bearer test-access-token"}
        ).status_code
        == 200
    )
    with secured_client.websocket_connect(f"/ws/projects/{pid}/progress") as connection:
        connection.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as raised:
            connection.receive_json()
        assert raised.value.code == 1008
    secured_client.close()
