"""Real service tests prove that API scrapes see other processes, not local globals."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from wenyi_api.adapters import create_context, postgres_repository
from wenyi_api.config import Settings
from wenyi_api.metrics import RedisCounters
from wenyi_backend.application import create_app
from wenyi_backend.context import use_context
from wenyi_backend.metrics import Metrics


@pytest.fixture
def redis_counters():
    url = os.environ.get("WENYI_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set WENYI_TEST_REDIS_URL for isolated Redis metric tests")
    namespace = "test:wenyi:metrics:" + uuid4().hex
    counters = RedisCounters(url, namespace=namespace, max_seen=4)
    try:
        yield counters
    finally:
        with Redis.from_url(url) as cache:
            cache.delete(*(f"{namespace}:{key}" for key in ("tasks", "retries", "seen", "order")))


@pytest.mark.integration
def test_redis_counters_deduplicate_concurrent_process_reports(redis_counters):
    def record(_):
        Metrics(
            RedisCounters(redis_counters.url, namespace=redis_counters.namespace, max_seen=4)
        ).record_retry(provider="fake", reason="timeout", call_id="call")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(20)))
    assert redis_counters.snapshot()["retries"] == {("fake", "timeout"): 1}
    metrics = Metrics(redis_counters)
    for index in range(10):
        metrics.record_retry(provider="fake", reason="timeout", call_id=f"extra-{index}")
    record(None)
    with Redis.from_url(redis_counters.url) as cache:
        assert cache.scard(f"{redis_counters.namespace}:seen") == 4
        assert cache.llen(f"{redis_counters.namespace}:order") == 4


@pytest.mark.integration
def test_api_scrapes_worker_process_counters_and_durable_queue(pg_pool, redis_counters, tmp_path):
    context = create_context(
        replace(
            Settings(),
            data_dir=str(tmp_path),
            redis_url=redis_counters.url,
            api_token=None,
        )
    )
    postgres_repository(context)._pool = pg_pool
    context = replace(context, metrics=Metrics(redis_counters))
    with use_context(context):
        pid = uuid4().hex
        with pg_pool.connection() as connection:
            connection.execute(
                "INSERT INTO projects(id,name,source_lang,target_lang) VALUES(%s,'test','en','zh')",
                (pid,),
            )
        queued = context.repository.create_job(pid, "translation", uuid4().hex)
        running = context.repository.create_job(pid, "export", uuid4().hex)
        context.repository.set_job_status(running, "running")
    subprocess.run(
        [
            sys.executable,
            "-c",
            """import sys
from wenyi_api.metrics import RedisCounters
from wenyi_backend.metrics import Metrics
metrics = Metrics(RedisCounters(sys.argv[1], namespace=sys.argv[2]))
metrics.record_task(status="error", code="pipeline_failed", identity="worker-run")
metrics.record_retry(provider="fake", reason="timeout", call_id="worker-call")
""",
            redis_counters.url,
            redis_counters.namespace,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with TestClient(create_app(context)) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert 'wenyi_tasks_total{code="pipeline_failed",status="error"} 1' in response.text
        assert 'wenyi_llm_retries_total{provider="fake",reason="timeout"} 1' in response.text
        assert "wenyi_queue_depth 1\n" in response.text
        with use_context(context):
            context.repository.set_job_status(queued, "running")
        assert "wenyi_queue_depth 0\n" in client.get("/metrics").text
