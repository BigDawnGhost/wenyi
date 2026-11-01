"""Desktop responses must expose exactly one correlation ID per header name."""

import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient
from wenyi_backend.errors import ApiError
from wenyi_backend.request_context import RequestContextMiddleware
from wenyi_core.observability import current_request_id
from wenyi_desktop.diagnostics import DiagnosticsMiddleware, Reporter
from wenyi_desktop.main import create_app, create_context


def test_desktop_layering_does_not_duplicate_correlation_headers():
    stream = io.StringIO()
    reporter = Reporter(stream)
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 500, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    inner = RequestContextMiddleware(app)
    outer = DiagnosticsMiddleware(inner, reporter)

    async def send(message):
        sent.append(message)

    asyncio.run(outer({"type": "http", "method": "GET", "path": "/", "headers": []}, None, send))
    reporter.close()
    keys = [key for key, _ in sent[0]["headers"]]
    assert keys.count(b"x-request-id") == 1, keys
    assert keys.count(b"x-error-id") == 1, keys
    headers = dict(sent[0]["headers"])
    record = json.loads(stream.getvalue().removeprefix("WENYI_DIAGNOSTIC "))
    assert record["attributes"]["request_id"] == headers[b"x-request-id"].decode()
    assert record["attributes"]["error_id"] == headers[b"x-error-id"].decode()


@pytest.mark.parametrize("handled", [False, True])
def test_assembled_desktop_correlates_safe_errors_and_keeps_cors(tmp_path, monkeypatch, handled):
    monkeypatch.setattr("wenyi_desktop.main.system_keyring", lambda: None)
    context = create_context(tmp_path)
    stream = io.StringIO()
    reporter = Reporter(stream)
    app = create_app(context=context, diagnostics=reporter)
    observed = []

    @app.get("/probe")
    def probe():
        observed.append(current_request_id())
        if handled:
            raise ApiError("invalid_configuration", 422, "Check the configuration")
        raise RuntimeError("synthetic-private-detail")

    try:
        # No lifespan: the HTTP stack does not need local worker threads.
        response = TestClient(app).get(
            "/probe",
            headers={"Origin": "tauri://localhost", "X-Request-ID": "synthetic-private-header"},
        )
    finally:
        reporter.close()
        context.repository.close()
    records = [
        json.loads(line.removeprefix("WENYI_DIAGNOSTIC "))
        for line in stream.getvalue().splitlines()
    ]
    record = next(item for item in records if item["event"].startswith("http_"))
    request_id = response.headers["x-request-id"]
    assert len(response.headers.get_list("x-request-id")) == 1
    assert observed == [request_id]
    assert record["attributes"]["request_id"] == request_id
    assert response.headers["access-control-allow-origin"] == "tauri://localhost"
    assert "X-Error-ID" in response.headers["access-control-expose-headers"]
    assert "synthetic-private" not in response.text + stream.getvalue()
    if handled:
        assert response.status_code == 422
        assert response.json()["request_id"] == request_id
        assert response.json()["code"] == "invalid_configuration"
        assert "x-error-id" not in response.headers
    else:
        assert response.status_code == 500
        assert len(response.headers.get_list("x-error-id")) == 1
        assert response.headers["x-error-id"] == request_id
        assert response.json()["error_id"] == record["attributes"]["error_id"] == request_id
        assert response.json()["code"] == "desktop_operation_failed"
