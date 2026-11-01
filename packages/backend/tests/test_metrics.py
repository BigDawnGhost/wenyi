"""Counters observe real events without introducing global state or failing a run."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_backend.metrics import MemoryCounters, Metrics, render


def test_task_identity_is_counted_once_across_reporters():
    store = MemoryCounters()
    first, second = Metrics(store), Metrics(store)
    first.record_task(status="error", code="project_busy", identity="project:run")
    second.record_task(status="error", code="project_busy", identity="project:run")
    text = render(second.snapshot(), depth=0)
    assert 'wenyi_tasks_total{code="project_busy",status="error"} 1' in text


def test_retry_waits_count_each_attempt_but_not_exhaustion_or_duplicates():
    metrics = Metrics()
    for attempt in (1, 1, 2):
        metrics.record_event(
            "llm_retry_wait",
            provider="fake",
            reason="empty_response",
            call_id="call",
            attempt=attempt,
        )
    metrics.record_event(
        "llm_retry_exhausted", provider="fake", reason="empty_response", call_id="call", attempt=3
    )
    assert metrics.snapshot()["retries"] == {("fake", "empty_response"): 2}


def test_a_retry_without_a_call_id_is_still_counted():
    metrics = Metrics()
    metrics.record_retry(provider="gemini", reason="connection")
    metrics.record_retry(provider="gemini", reason="connection")
    assert metrics.snapshot()["retries"] == {("gemini", "connection"): 2}


def test_deduplication_is_atomic_under_concurrent_reports():
    metrics = Metrics()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: metrics.record_retry(provider="fake", reason="timeout", call_id="one"),
                range(40),
            )
        )
    assert metrics.snapshot()["retries"] == {("fake", "timeout"): 1}


def test_deduplication_is_bounded_without_discarding_all_recent_ids():
    store = MemoryCounters(max_seen=4)
    metrics = Metrics(store)
    for index in range(10):
        metrics.record_retry(provider="fake", reason="timeout", call_id=f"call-{index}")
    metrics.record_retry(provider="fake", reason="timeout", call_id="call-9")
    assert len(store._seen) == 4
    assert metrics.snapshot()["retries"] == {("fake", "timeout"): 10}


def test_independent_applications_do_not_share_counters():
    first, second = Metrics(), Metrics()
    first.record_task(status="done", code="none")
    assert second.snapshot() == {"tasks": {}, "retries": {}}
    snapshot = first.snapshot()
    snapshot["tasks"].clear()
    assert first.snapshot()["tasks"] == {("done", "none"): 1}


def test_recording_survives_a_broken_store(caplog):
    store = Mock()
    store.increment.side_effect = RuntimeError("synthetic private transport detail")
    metrics = Metrics(store)
    metrics.record_task(status="done", code="none")
    metrics.record_retry(provider="fake", reason="timeout")
    assert "synthetic private" not in caplog.text


def test_exposition_escapes_label_values():
    metrics = Metrics()
    metrics.record_retry(provider='fake"\\', reason="first\nsecond")
    text = render(metrics.snapshot(), depth=3)
    assert 'provider="fake\\"\\\\"' in text
    assert 'reason="first\\nsecond"' in text
    assert text.endswith("wenyi_queue_depth 3\n")


def test_endpoint_reads_its_own_catalog_and_requires_configured_token(backend_context):
    context = replace(backend_context, api_token="fixture-token")
    context.metrics.record_task(status="error", code="queue_unavailable")
    context.repository.queued_job_count.return_value = 3
    with TestClient(create_app(context)) as client:
        assert client.get("/metrics").status_code == 401
        response = client.get("/metrics", headers={"Authorization": "Bearer fixture-token"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'wenyi_tasks_total{code="queue_unavailable",status="error"} 1' in response.text
    assert "wenyi_queue_depth 3\n" in response.text


def test_failed_scrape_is_unavailable_instead_of_reporting_zero(backend_context):
    store = Mock()
    store.snapshot.side_effect = RuntimeError("synthetic private connection")
    context = replace(backend_context, metrics=Metrics(store))
    with TestClient(create_app(context)) as client:
        response = client.get("/metrics")
    assert response.status_code == 503
    assert "synthetic private" not in response.text
    assert "wenyi_queue_depth 0" not in response.text
