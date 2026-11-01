"""Desktop metrics belong to one app and use its durable local job catalog."""

from contextlib import closing

from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_desktop.main import create_context


def test_desktop_scrapes_are_isolated_and_follow_real_job_transitions(tmp_path):
    first = create_context(tmp_path / "first")
    second = create_context(tmp_path / "second")
    with closing(first.repository), closing(second.repository):
        pid = first.repository.create_project("test", "en", "zh", {})
        workflow = first.repository.create_job(pid, "translation", "workflow")
        export = first.repository.create_job(pid, "export", "export")
        first.repository.create_job(pid, "review", "old", status="done")
        first.metrics.record_task(status="done", code="none", identity="completed-run")
        with TestClient(create_app(first)) as client, TestClient(create_app(second)) as other:
            response = client.get("/metrics")
            assert response.status_code == 200
            assert 'wenyi_tasks_total{code="none",status="done"} 1' in response.text
            assert "wenyi_queue_depth 2\n" in response.text
            first.repository.set_job_status(workflow, "running")
            assert "wenyi_queue_depth 1\n" in client.get("/metrics").text
            first.repository.set_job_status(export, "error")
            assert "wenyi_queue_depth 0\n" in client.get("/metrics").text
            isolated = other.get("/metrics")
            assert isolated.status_code == 200
            assert "wenyi_tasks_total{" not in isolated.text
            assert "wenyi_queue_depth 0\n" in isolated.text
