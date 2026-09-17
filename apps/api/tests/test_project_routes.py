"""Real PostgreSQL API/worker contracts, with deterministic offline model responses."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_storage_pg_integration import pg_pool  # noqa: F401
from tests.fake_llm import MeteredFakeClient, routing_handler
from type_helpers import must
from wenyi_api import dal, job_service
from wenyi_api.db import pool as pool_module
from wenyi_api.main import create_app
from wenyi_api.project_service import storage_for
from wenyi_api.routers import export
from wenyi_api.workers import tasks


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
    # Seed existing projects for endpoint tests; public creation requires a source.
    return dal.create_project("Book", source, target, {"template": "标准翻译"})


def test_creation_requires_an_uploaded_source(api):
    client, queue = api
    before = dal.list_projects()
    response = client.post("/projects", json={"name": "Book"})
    assert response.status_code == 422
    assert dal.list_projects() == before
    assert queue == []


@pytest.mark.parametrize("prepare", [False, True])
def test_creation_uploads_source_and_runs_selected_setup(api, prepare):
    client, queue = api
    response = client.post(
        "/projects",
        data={"project": json.dumps({"name": "Book", "source_lang": "en", "prepare": prepare})},
        files={"file": ("book.html", b"<h1>Chapter One</h1><p>A book begins.</p>")},
    )
    assert response.status_code == 201, response.text
    project = response.json()
    pid = project["id"]
    assert project["status"] == ("preparing" if prepare else "parsing")
    assert project["source_meta"]["original_filename"] == "book.html"
    assert queue[0][0] == ("run_prepare" if prepare else "run_parse")
    assert must(dal.get_project(pid))["source_sha256"]
    execute_next(api)
    assert client.get(f"/projects/{pid}/preview").status_code == 200
    assert client.get(f"/projects/{pid}").json()["status"] == (
        "prepared" if prepare else "uploaded"
    )
    assert all(ch["status"] != "done" for ch in dal.chapter_summaries(pid))


@pytest.mark.parametrize(
    "metadata,filename,content",
    [
        ({"name": "Book"}, "book.txt", b""),
        ({"name": "Book"}, "book.exe", b"contents"),
        ({"name": "  "}, "book.txt", b"contents"),
        ({"name": "Book", "source_lang": "en", "target_lang": "en"}, "book.txt", b"text"),
        ({"name": "Book", "target_lang": "invalid"}, "book.txt", b"text"),
        ({"name": "Book", "prepare": True}, "book.srt", b"subtitles"),
    ],
)
def test_invalid_creation_never_publishes_a_project(api, metadata, filename, content, tmp_path):
    client, queue = api
    before = dal.list_projects()
    response = client.post(
        "/projects", data={"project": json.dumps(metadata)}, files={"file": (filename, content)}
    )
    assert response.status_code == 422, response.text
    assert dal.list_projects() == before
    assert queue == []
    assert not list((tmp_path / "data").rglob("source-*"))


def test_creation_queue_failure_keeps_source_and_resumes_preparation(api, monkeypatch):
    client, queue = api
    before = dal.list_projects()
    actual_enqueue = job_service.enqueue

    async def fail(*args, **kwargs):
        raise ConnectionError("redis offline")

    monkeypatch.setattr(job_service, "enqueue", fail)
    response = client.post(
        "/projects",
        data={"project": json.dumps({"name": "Book", "prepare": True})},
        files={"file": ("book.html", b"<h1>Chapter One</h1><p>The story begins.</p>")},
    )
    assert response.status_code == 201, response.text
    project = response.json()
    pid = project["id"]
    assert project["status"] == "error" and project["error"]
    assert project["source_meta"]["original_filename"] == "book.html"
    assert len(dal.list_projects()) == len(before) + 1 and queue == []
    monkeypatch.setattr(job_service, "enqueue", actual_enqueue)
    assert client.post(f"/projects/{pid}/resume").json()["kind"] == "prepare"
    execute_next(api)
    assert client.get(f"/projects/{pid}").json()["status"] == "prepared"
    assert client.get(f"/projects/{pid}/preview").status_code == 200


def test_creation_database_failure_removes_its_uploaded_file(api, monkeypatch, tmp_path):
    client, queue = api
    before = dal.list_projects()

    def fail(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(dal, "create_project", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        client.post(
            "/projects",
            data={"project": json.dumps({"name": "Book"})},
            files={"file": ("book.txt", b"A book begins.")},
        )
    assert dal.list_projects() == before and queue == []
    assert not list((tmp_path / "data").rglob("source-*"))


def test_interrupted_upload_removes_partial_source(api, monkeypatch, tmp_path):
    from io import BytesIO

    from fastapi import UploadFile
    from wenyi_api.source_upload import save_source

    before = dal.list_projects()
    file = UploadFile(file=BytesIO(b"first block"), filename="book.txt")
    reads = 0

    async def interrupted_read(size):
        nonlocal reads
        reads += 1
        if reads > 1:
            raise OSError("upload interrupted")
        return b"first block"

    monkeypatch.setattr(file, "read", interrupted_read)
    with pytest.raises(OSError, match="upload interrupted"):
        asyncio.run(save_source("partial-upload", file))
    assert not list((tmp_path / "data").rglob("source-*"))
    assert dal.list_projects() == before


def test_creation_saves_pdf_parser_before_queueing(api):
    client, queue = api
    response = client.post(
        "/projects",
        data={"project": json.dumps({"name": "PDF", "pdf_backend": "babeldoc"})},
        files={"file": ("book.pdf", b"%PDF-test-fixture")},
    )
    assert response.status_code == 201, response.text
    project = must(dal.get_project(response.json()["id"]))
    assert project["config"]["pipeline"]["pdf_backend"] == "babeldoc"
    run_id = queue[0][1]["run_id"]
    assert tasks._build_config_for(project["id"], run_id).pipeline.pdf_backend == "babeldoc"


def test_subtitle_creation_only_parses_source(api):
    client, queue = api
    response = client.post(
        "/projects",
        data={"project": json.dumps({"name": "Subtitles"})},
        files={"file": ("video.srt", b"1\n00:00:00,100 --> 00:00:01,200\nHello\n")},
    )
    assert response.status_code == 201, response.text
    assert queue[0][0] == "run_parse"
    pid = response.json()["id"]
    execute_next(api)
    assert client.get(f"/projects/{pid}/preview").json()["total_word_count"] == 1
    assert not must(dal.get_project(pid))["initialized"]


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
    assert client.get(f"/projects/{pid}").json()["error"] is None
    assert must(dal.get_job_by_arq_id(run_id))["error"] is None
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
    # Resuming subtitles generates new files and applies the same export retention policy.
    assert client.post(f"/projects/{pid}/translate").status_code == 200
    execute_next(api)
    with dal.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT path FROM exports WHERE project_id=%s AND status='done'", (pid,)
        ).fetchall()
    assert len(rows) == 5
    from pathlib import Path

    files = list((Path(tasks.settings.data_dir) / pid / "exports").rglob("*.srt"))
    assert len(files) == 5
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
