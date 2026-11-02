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
# Added by the export domain migration in T-15.
EXPORT_NOT_FOUND = "export_not_found"
SOURCE_NOT_READY = "source_not_ready"
SETTINGS_CONFLICT = "settings_conflict"
# Added by the review domain migration in T-15.
CHAPTER_NOT_FOUND = "chapter_not_found"
SEGMENT_NOT_FOUND = "segment_not_found"
INVALID_CONFIGURATION = "invalid_configuration"
UNSUPPORTED_CLIENT = "unsupported_client"


class ApiError(HTTPException):
    """A client-visible failure identified by a stable, machine-readable code.

    ``message`` is the previous human-readable text, so the legacy ``detail``
    field keeps its value while clients migrate to ``code``.

    It subclasses FastAPI's ``HTTPException`` so existing ``except HTTPException``
    guards and tests that assert on that type keep working during the migration
    window; ``status_code`` and ``detail`` are the inherited equivalents of
    ``status`` and ``message``. FastAPI re-exports its own subclass, so inheriting
    the Starlette base would not satisfy ``isinstance(exc, fastapi.HTTPException)``.
    """

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(status_code=status, detail=message)
        self.code = code
        self.message = message

    @property
    def status(self) -> int:
        return self.status_code

    def __str__(self) -> str:
        # HTTPException renders "status: detail", which would change what a
        # caller matching on the message sees. Keep the message alone, exactly
        # as the pre-migration exception did.
        return self.message


def error_envelope(*, code: str, message: str, request_id: str | None = None) -> dict[str, Any]:
    """Build the response body for a coded failure.

    ``code`` is the stable identity and ``message`` is what a person reads.
    """
    body: dict[str, Any] = {"code": code, "message": message}
    if request_id:
        body["request_id"] = request_id
    return body


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Render an :class:`ApiError` with its code and message."""
    body = error_envelope(code=exc.code, message=exc.message, request_id=current_request_id())
    return JSONResponse(body, status_code=exc.status)


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Unrecognised failures keep FastAPI's original ``{"detail": ...}`` shape.

    A user-facing failure must raise :class:`ApiError` so the response carries a
    stable code; this handler only covers what is left over.
    """
    headers = getattr(exc, "headers", None)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)


def describe_status(status: int) -> str:
    """Return the standard reason phrase for a status code, for log messages."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return str(status)


__all__ = [
    "INVALID_CONFIGURATION",
    "PROJECT_BUSY",
    "PROJECT_NOT_FOUND",
    "QUEUE_UNAVAILABLE",
    "UNSUPPORTED_CLIENT",
    "ApiError",
    "api_error_handler",
    "describe_status",
    "error_envelope",
    "http_exception_handler",
]
