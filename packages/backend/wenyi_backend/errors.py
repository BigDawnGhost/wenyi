"""Stable machine-readable error codes on top of the existing HTTP contract."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from wenyi_core.observability import current_request_id

# The first batch of codes covers the failures clients already branch on today.
PROJECT_NOT_FOUND = "project_not_found"
PROJECT_BUSY = "project_busy"
QUEUE_UNAVAILABLE = "queue_unavailable"
INVALID_CONFIGURATION = "invalid_configuration"
UNSUPPORTED_CLIENT = "unsupported_client"


class ApiError(Exception):
    """A client-visible failure identified by a stable, machine-readable code.

    ``message`` is the previous human-readable text, so the legacy ``detail``
    field keeps its value while clients migrate to ``code``.
    """

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


# Legacy (status, detail) pairs translated into codes during the dual-write
# window. Only text a client can already observe is listed; anything else keeps
# the original ``detail``-only response shape.
LEGACY_ERRORS: tuple[tuple[int, str, str], ...] = (
    (404, "project not found", PROJECT_NOT_FOUND),
    (409, "project already has a running task", PROJECT_BUSY),
    (503, "Task queue is unavailable; retry the operation", QUEUE_UNAVAILABLE),
    (503, "Export queue is unavailable; retry the operation", QUEUE_UNAVAILABLE),
    (422, "Invalid connection settings", INVALID_CONFIGURATION),
    (422, "Invalid model list request", INVALID_CONFIGURATION),
    (422, "Unsupported client", UNSUPPORTED_CLIENT),
)

_LEGACY_LOOKUP: dict[tuple[int, str], str] = {
    (status, detail): code for status, detail, code in LEGACY_ERRORS
}


def code_for(status: int, detail: Any) -> str | None:
    """Return the code recorded for a legacy (status, detail) pair, if any."""
    if not isinstance(detail, str):
        return None
    return _LEGACY_LOOKUP.get((status, detail))


def error_envelope(*, code: str, message: str, request_id: str | None = None) -> dict[str, Any]:
    """Build the response body that carries both the new and the legacy fields."""
    body: dict[str, Any] = {"code": code, "message": message}
    if request_id:
        body["request_id"] = request_id
    # Dual write: clients that only read ``detail`` keep working unchanged.
    body["detail"] = message
    return body


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Render an :class:`ApiError` with its code and the legacy detail field."""
    body = error_envelope(code=exc.code, message=exc.message, request_id=current_request_id())
    return JSONResponse(body, status_code=exc.status)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Translate known legacy failures into codes while preserving the old shape.

    Unrecognised details keep FastAPI's original ``{"detail": ...}`` response so
    no existing client can observe a change it did not opt into.
    """
    headers = getattr(exc, "headers", None)
    code = code_for(exc.status_code, exc.detail)
    if code is None:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)
    body = error_envelope(code=code, message=exc.detail, request_id=current_request_id())
    return JSONResponse(body, status_code=exc.status_code, headers=headers)


def describe_status(status: int) -> str:
    """Return the standard reason phrase for a status code, for log messages."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return str(status)


__all__ = [
    "INVALID_CONFIGURATION",
    "LEGACY_ERRORS",
    "PROJECT_BUSY",
    "PROJECT_NOT_FOUND",
    "QUEUE_UNAVAILABLE",
    "UNSUPPORTED_CLIENT",
    "ApiError",
    "api_error_handler",
    "code_for",
    "describe_status",
    "error_envelope",
    "http_exception_handler",
]
