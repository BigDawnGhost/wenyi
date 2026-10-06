"""Web adapters are owned by each test's explicit application context."""

import os
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg_pool import ConnectionPool
from tests.fake_llm import MeteredFakeClient, routing_handler
from wenyi_api import dal
from wenyi_api.adapters import create_context, postgres_repository
from wenyi_api.config import Settings
from wenyi_api.main import create_app
from wenyi_api.storage_pg import PostgresStorage
from wenyi_backend import job_service, project_service
from wenyi_backend.context import ContextMiddleware, current_context, use_context
from wenyi_backend.routers import chapters, export, glossary, report, review, style, subtitles
from wenyi_core.storage.file import FileStorage


@pytest.fixture(autouse=True)
def web_context(tmp_path):
    context = create_context(replace(Settings(), data_dir=str(tmp_path), api_token=None))
    with use_context(context):
        yield context


@pytest.fixture(scope="module")
def pg_pool():
    """Create an isolated schema only when an explicit test service is configured."""
    dsn = os.environ.get("WENYI_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set WENYI_TEST_DATABASE_URL for real PostgreSQL storage tests")
    schema = "test_wenyi_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=8,
        open=True,
        kwargs={"options": f"-c search_path={schema},public", "client_encoding": "UTF8"},
    )
    pool.wait()
    try:
        schema_sql = Path(__file__).parents[1] / "wenyi_api" / "db" / "schema.sql"
        with pool.connection() as conn:
            conn.execute(cast(Any, schema_sql.read_text(encoding="utf-8")))
        yield pool
    finally:
        pool.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def pg_storage(pg_pool, tmp_path):
    pid = uuid.uuid4().hex
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,source_lang,target_lang) VALUES(%s,'test','en','zh')",
            (pid,),
        )
    return PostgresStorage(pid, pg_pool, run_dir=str(tmp_path / "resources"))


@pytest.fixture(params=["file", pytest.param("postgres", marks=pytest.mark.integration)])
def storage(request, tmp_path):
    if request.param == "postgres":
        result = request.getfixturevalue("pg_storage")
    else:
        result = FileStorage(str(tmp_path / "file-state"))
    yield result
    result.close()


def pytest_collection_modifyitems(items):
    """Service-backed tests opt in via transitive pg_pool fixture dependencies."""
    for item in items:
        if "pg_pool" in item.fixturenames:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def api(monkeypatch, pg_pool, tmp_path):
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
    monkeypatch.setattr(
        factory, "build_client", lambda cfg: MeteredFakeClient(handler=routing_handler)
    )
    context = replace(
        create_context(overrides),
        build_client=lambda cfg: factory.build_client(cfg),
    )
    postgres_repository(context)._pool = pg_pool
    queue = []

    async def enqueue(name, **kwargs):
        queue.append((name, kwargs))
        return SimpleNamespace(job_id=kwargs["_job_id"])

    monkeypatch.setattr(job_service, "enqueue", enqueue)
    monkeypatch.setattr(export, "enqueue", enqueue)
    client = TestClient(create_app(context=context))
    with use_context(context):
        yield client, queue
    client.close()


@pytest.fixture
def domain_client(pg_storage, pg_pool, monkeypatch):
    monkeypatch.setattr(dal, "get_pool", lambda: pg_pool)
    monkeypatch.setattr(project_service, "storage_for", lambda pid: pg_storage)
    for module in (chapters, glossary, report, review, style, subtitles):
        monkeypatch.setattr(module, "storage_for", lambda pid: pg_storage)
    postgres_repository(current_context())._pool = pg_pool
    queued = []

    async def start(pid, kind, *, params=None):
        queued.append({"pid": pid, "kind": kind, "params": params})
        return {"job_id": "queued-task", "project_id": pid, "kind": kind}

    monkeypatch.setattr(chapters, "start_job", start)
    monkeypatch.setattr(review, "start_job", start)
    app = FastAPI()
    app.add_middleware(ContextMiddleware, context=current_context())
    for module in (chapters, glossary, report, review, style, subtitles):
        app.include_router(module.router)
    with TestClient(app) as client:
        yield client, pg_storage, queued
