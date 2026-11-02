"""Error codes are additive: every old client still sees its ``detail`` text."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from wenyi_backend.errors import (
    PROJECT_BUSY,
    ApiError,
    api_error_handler,
    error_envelope,
    http_exception_handler,
)


def _client():
    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)

    @app.get("/busy")
    def busy():
        raise HTTPException(409, "project already has a running task")

    @app.get("/missing")
    def missing():
        raise HTTPException(404, "project not found")

    @app.get("/unknown")
    def unknown():
        raise HTTPException(418, "nothing depends on this text")

    @app.get("/api-error")
    def api_error():
        raise ApiError(PROJECT_BUSY, 409, "project already has a running task")

    return TestClient(app)


def test_a_bare_http_exception_keeps_the_original_shape():
    """Text alone is no longer translated into a code; raising ApiError is.

    The wording used to be matched against a table, which meant any copy edit
    silently dropped the code. Both routes below now answer with the plain
    FastAPI shape, and the coded path is the one that raises ApiError.
    """
    for path in ("/busy", "/missing"):
        body = _client().get(path).json()
        assert "code" not in body, path
        assert set(body) == {"detail"}, path


def test_a_raised_api_error_is_reported_by_code():
    response = _client().get("/api-error")
    assert response.status_code == 409
    assert response.json()["code"] == PROJECT_BUSY


def test_unrecognised_failure_keeps_the_original_detail_only_shape():
    response = _client().get("/unknown")
    assert response.status_code == 418
    assert response.json() == {"detail": "nothing depends on this text"}


def test_api_error_renders_its_code_alongside_the_legacy_detail():
    body = _client().get("/api-error").json()
    assert body["code"] == PROJECT_BUSY
    assert body["detail"] == "project already has a running task"


def test_envelope_omits_request_id_when_absent_but_always_writes_detail():
    assert error_envelope(code="x", message="m") == {"code": "x", "message": "m", "detail": "m"}
    with_id = error_envelope(code="x", message="m", request_id="r1")
    assert with_id["request_id"] == "r1"
