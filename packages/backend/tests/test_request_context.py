"""Every HTTP response must carry a correlation ID that matches its log line."""

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from wenyi_backend.request_context import (
    ERROR_ID_HEADER,
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
)
from wenyi_core import observability


@pytest.fixture(autouse=True)
def restore_logging():
    import logging

    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
    for handler in before:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


def _client(stream=None):
    app = FastAPI()

    @app.get("/ok")
    def ok():
        import logging

        logging.getLogger("wenyi.test").info("handled request")
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    app.add_middleware(RequestContextMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def _records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_response_carries_a_request_id():
    response = _client().get("/ok")
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]


def test_inbound_request_id_is_reused():
    response = _client().get("/ok", headers={REQUEST_ID_HEADER: "caller-supplied"})
    assert response.headers[REQUEST_ID_HEADER] == "caller-supplied"


def test_server_error_also_carries_an_error_id():
    response = _client().get("/boom")
    assert response.status_code == 500
    request_id = response.headers[REQUEST_ID_HEADER]
    assert response.headers[ERROR_ID_HEADER] == request_id


def test_log_records_share_the_request_id():
    stream = io.StringIO()
    observability.configure_logging(level="INFO", json=True, service="tests", stream=stream)
    response = _client().get("/ok")
    request_id = response.headers[REQUEST_ID_HEADER]
    matching = [row for row in _records(stream) if row["logger"] == "wenyi.test"]
    assert len(matching) == 1, _records(stream)
    assert matching[0]["request_id"] == request_id


def test_unhandled_failure_is_logged_with_the_correlation_id():
    """The middleware owns the 500, so it is the only place that can log it."""
    stream = io.StringIO()
    observability.configure_logging(level="INFO", json=True, service="tests", stream=stream)
    response = _client().get("/boom")
    assert response.status_code == 500
    error_id = response.headers[ERROR_ID_HEADER]
    failures = [row for row in _records(stream) if row["logger"] == "wenyi_backend.request_context"]
    assert len(failures) == 1, _records(stream)
    assert failures[0]["level"] == "ERROR"
    assert failures[0]["request_id"] == error_id
    assert "kaboom" in failures[0]["exception"]


@pytest.mark.parametrize("status", [200, 404, 500])
def test_every_response_status_is_stamped(status):
    path = {200: "/ok", 404: "/missing", 500: "/boom"}[status]
    response = _client().get(path)
    assert response.status_code == status
    assert response.headers[REQUEST_ID_HEADER]
    has_error_id = ERROR_ID_HEADER in response.headers
    assert has_error_id == (status >= 500)


def test_unhandled_failure_still_raises_after_serving_a_correlated_500():
    """Starlette re-raises after building its 500; the middleware must too.

    Swallowing the exception would hide the real failure from the server log
    and from any test client that asserts on it, so the response is emitted
    and the original exception still propagates.
    """
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    app.add_middleware(RequestContextMiddleware)
    client = TestClient(app, raise_server_exceptions=True)
    with pytest.raises(RuntimeError, match="kaboom"):
        client.get("/boom")


def test_client_can_still_request_a_quiet_500():
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("kaboom")

    app.add_middleware(RequestContextMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    assert response.headers[ERROR_ID_HEADER]
