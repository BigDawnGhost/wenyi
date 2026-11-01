"""Error codes are additive: every old client still sees its ``detail`` text."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from wenyi_backend.errors import (
    CHAPTER_NOT_FOUND,
    EXPORT_NOT_FOUND,
    INVALID_CONFIGURATION,
    LEGACY_ERRORS,
    PROJECT_BUSY,
    PROJECT_NOT_FOUND,
    QUEUE_UNAVAILABLE,
    SEGMENT_NOT_FOUND,
    SETTINGS_CONFLICT,
    SOURCE_NOT_READY,
    UNSUPPORTED_CLIENT,
    ApiError,
    api_error_handler,
    code_for,
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


def test_known_legacy_failure_gains_a_code_and_keeps_its_detail():
    body = _client().get("/busy").json()
    assert body["code"] == PROJECT_BUSY
    assert body["detail"] == "project already has a running task"
    assert body["message"] == body["detail"]


def test_missing_project_is_reported_by_code():
    response = _client().get("/missing")
    assert response.status_code == 404
    assert response.json()["code"] == PROJECT_NOT_FOUND


def test_unrecognised_failure_keeps_the_original_detail_only_shape():
    response = _client().get("/unknown")
    assert response.status_code == 418
    assert response.json() == {"detail": "nothing depends on this text"}


def test_api_error_renders_its_code_alongside_the_legacy_detail():
    body = _client().get("/api-error").json()
    assert body["code"] == PROJECT_BUSY
    assert body["detail"] == "project already has a running task"


@pytest.mark.parametrize("status, detail, code", LEGACY_ERRORS)
def test_every_registered_legacy_pair_maps_to_its_code(status, detail, code):
    assert code_for(status, detail) == code


def test_unknown_or_non_string_details_do_not_get_a_code():
    assert code_for(500, "boom") is None
    assert code_for(404, {"nested": "detail"}) is None


def test_envelope_omits_request_id_when_absent_but_always_writes_detail():
    assert error_envelope(code="x", message="m") == {"code": "x", "message": "m", "detail": "m"}
    with_id = error_envelope(code="x", message="m", request_id="r1")
    assert with_id["request_id"] == "r1"


def test_first_batch_codes_are_the_agreed_five():
    codes = {code for _, _, code in LEGACY_ERRORS}
    # The first batch stays present and unchanged; later domain migrations may
    # add codes (export added export_not_found), never remove the original five.
    assert {
        PROJECT_NOT_FOUND,
        PROJECT_BUSY,
        QUEUE_UNAVAILABLE,
        INVALID_CONFIGURATION,
        UNSUPPORTED_CLIENT,
    } <= codes


def test_domain_migrations_added_only_the_expected_codes():
    codes = {code for _, _, code in LEGACY_ERRORS}
    # Every code beyond the first batch must be one a migration deliberately
    # introduced, so an accidental borrow (or a typo) fails this list.
    assert codes - {
        PROJECT_NOT_FOUND,
        PROJECT_BUSY,
        QUEUE_UNAVAILABLE,
        INVALID_CONFIGURATION,
        UNSUPPORTED_CLIENT,
    } == {
        EXPORT_NOT_FOUND,
        SOURCE_NOT_READY,
        SETTINGS_CONFLICT,
        CHAPTER_NOT_FOUND,
        SEGMENT_NOT_FOUND,
    }
