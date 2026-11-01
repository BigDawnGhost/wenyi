"""The metric registry must count once, stay quiet, and never raise."""

import pytest
from fastapi.testclient import TestClient
from wenyi_backend import metrics
from wenyi_backend.application import create_app


@pytest.fixture(autouse=True)
def clean_registry():
    metrics.reset()
    yield
    metrics.reset()


def test_a_task_is_counted_by_status_and_code():
    metrics.record_task(status="error", code="project_busy")
    text = metrics.render()
    assert 'wenyi_tasks_total{code="project_busy",status="error"} 1' in text


def test_the_same_call_is_counted_once():
    """The event ledger already merges a retried call; counting again would lie."""
    metrics.record_retry(provider="openai", reason="timeout", call_id="call-1")
    metrics.record_retry(provider="openai", reason="timeout", call_id="call-1")
    metrics.record_retry(provider="openai", reason="timeout", call_id="call-2")
    text = metrics.render()
    assert 'wenyi_llm_retries_total{provider="openai",reason="timeout"} 2' in text


def test_a_retry_without_a_call_id_is_still_counted():
    metrics.record_retry(provider="gemini", reason="connection")
    metrics.record_retry(provider="gemini", reason="connection")
    text = metrics.render()
    assert 'wenyi_llm_retries_total{provider="gemini",reason="connection"} 2' in text


def test_the_dedupe_set_does_not_grow_without_limit():
    metrics._REGISTRY.max_seen = 4
    for index in range(10):
        metrics.record_retry(provider="openai", reason="timeout", call_id=f"call-{index}")
    assert len(metrics._REGISTRY.seen_calls) <= 4
    assert 'wenyi_llm_retries_total{provider="openai",reason="timeout"} 10' in metrics.render()


def test_the_exposition_is_scrape_shaped():
    metrics.record_queue_depth(depth=3)
    text = metrics.render()
    assert "# TYPE wenyi_tasks_total counter" in text
    assert "# TYPE wenyi_llm_retries_total counter" in text
    assert "# TYPE wenyi_queue_depth gauge" in text
    assert text.endswith("\n")
    for line in text.splitlines():
        assert line.startswith("#") or " " in line, line


def test_recording_survives_a_broken_registry(monkeypatch):
    """A counter is observability: it must never fail the run it measures."""

    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("registry is gone")

    monkeypatch.setattr(metrics, "_REGISTRY", Exploding())
    metrics.record_task(status="ok", code="none")
    metrics.record_retry(provider="openai", reason="timeout")
    metrics.record_queue_depth(depth=1)


def test_the_endpoint_returns_the_exposition(backend_context):
    metrics.record_task(status="error", code="queue_unavailable")
    with TestClient(create_app(backend_context)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'wenyi_tasks_total{code="queue_unavailable",status="error"} 1' in response.text
