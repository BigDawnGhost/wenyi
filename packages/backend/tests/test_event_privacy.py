"""Historical internal tracebacks must not escape through the public event API."""

from unittest.mock import Mock

from fastapi.testclient import TestClient
from wenyi_backend.application import create_app


def test_event_api_excludes_internal_tracebacks(backend_context):
    backend_context.repository.get_project.return_value = {"id": "project"}
    store = Mock()
    store.list_events.return_value = [
        {
            "_id": 1,
            "event": "pipeline_error",
            "error": "Operation failed",
            "error_type": "RuntimeError",
            "traceback": "synthetic private path and chained exception",
        }
    ]
    backend_context.storage_for.return_value = store
    with TestClient(create_app(backend_context)) as client:
        response = client.get("/projects/project/events")
    assert response.status_code == 200
    assert response.json()[0]["payload"] == {
        "error": "Operation failed",
        "error_type": "RuntimeError",
    }
    assert "synthetic private" not in response.text
