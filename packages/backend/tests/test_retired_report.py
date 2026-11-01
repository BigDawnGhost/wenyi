"""The retired project report has no HTTP surface or workflow capability."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app
from wenyi_backend.workers import tasks
from wenyi_core.config import Config


def test_report_routes_are_absent(backend_context):
    app = create_app(backend_context)
    assert "/projects/{pid}/report" not in app.openapi()["paths"]
    with TestClient(app) as client:
        assert client.get("/projects/p/report").status_code == 404
        assert client.post("/projects/p/report").status_code == 404
    backend_context.storage_for.assert_not_called()
    backend_context.read_storage_for.assert_not_called()


@pytest.mark.parametrize("kind", ["translation", "review"])
@pytest.mark.parametrize("review", [False, True])
def test_book_tasks_no_longer_generate_reports(monkeypatch, kind, review):
    orch = Mock(spec=["run_review", "run_steps"])
    monkeypatch.setattr("wenyi_core.pipeline.orchestrator.Orchestrator", lambda *a, **kw: orch)
    monkeypatch.setattr(tasks, "_resolve_source", lambda pid: "book.txt")
    config = Config.from_dict({"llm": {"preset": "fake"}, "pipeline": {"review": review}})
    progress = Mock()

    status = tasks._book_operation(kind, "p", Mock(), config, Mock(), progress, {})

    if kind == "review":
        assert status == "reviewed"
        orch.run_review.assert_called_once_with("book.txt", progress=progress)
        orch.run_steps.assert_not_called()
    else:
        assert status == "done"
        steps = {"translate", "review"} if review else {"translate"}
        orch.run_steps.assert_called_once_with("book.txt", steps, progress=progress)
        orch.run_review.assert_not_called()
