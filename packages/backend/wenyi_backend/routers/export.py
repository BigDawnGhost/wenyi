"""Queue independent exports and serve authenticated, project-scoped downloads."""

from __future__ import annotations

import asyncio
import mimetypes
from functools import cache
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config_documents import runtime_document
from ..context import current_context
from ..errors import (
    EXPORT_NOT_FOUND,
    INVALID_CONFIGURATION,
    QUEUE_UNAVAILABLE,
    ApiError,
)
from ..export_plan import export_input, resolve_export
from ..export_response import ExportResponse
from ..schemas import AssembleEnqueued, ExportOut, ExportRequest
from ..workers import enqueue
from .dependencies import effective_config, require_project, storage_for

router = APIRouter(prefix="/projects/{pid}/exports", tags=["export"])


@router.get("", response_model=list[ExportOut])
def list_exports(pid: str) -> list[dict]:
    require_project(pid)
    return current_context().exports.list_exports(pid)


def _resolve_input(project: dict, body: ExportRequest) -> tuple[str, dict, dict]:
    @cache
    def config():
        return effective_config(project)

    store = storage_for(project["id"])
    try:
        fmt, options = resolve_export(
            project, export_input(project, store, lambda _: config()), body
        )
        return fmt, options, runtime_document(config())
    except ValueError as error:
        raise ApiError(INVALID_CONFIGURATION, 422, str(error)) from None
    finally:
        store.close()


async def enqueue_export(pid: str, body: ExportRequest, *, kind: str = "export") -> dict:
    project = require_project(pid)
    # Loading an uninitialized book's saved parse can be expensive. Keep the HTTP
    # event loop free and open/close the adapter on the same worker thread.
    fmt, options, snapshot = await asyncio.to_thread(_resolve_input, project, body)
    source = {
        field: project.get(field)
        for field in ("source_path", "source_sha256", "source_meta", "fmt", "initialized")
    }
    exports = current_context().exports
    try:
        admission = exports.admit_export(pid, fmt, options, uuid4().hex, snapshot, source=source)
    except (ApiError, StarletteHTTPException):
        # admit_export already carries a code; re-raise it untouched instead of
        # letting the generic handler relabel it as a queue failure.
        raise
    except Exception as error:
        raise ApiError(
            QUEUE_UNAVAILABLE, 503, "Export queue is unavailable; retry the operation"
        ) from error
    run_id, export_id = admission.run_id, admission.export_id
    response = {"job_id": run_id, "project_id": pid, "kind": kind, "export_id": export_id}
    if not admission.created:
        return response
    try:
        job = await enqueue(
            "run_export",
            _job_id=run_id,
            project_id=pid,
            run_id=run_id,
            export_id=export_id,
            fmt=fmt,
            **(
                {"parsed_source_sha256": source["source_sha256"]}
                if not source["initialized"]
                else {}
            ),
            **options,
        )
        if job is None:
            raise RuntimeError("The queue did not accept this export")
    except Exception as error:
        exports.fail_export_admission(admission.job_id, str(error))
        raise ApiError(
            QUEUE_UNAVAILABLE, 503, "Export queue is unavailable; retry the operation"
        ) from error
    return response


@router.post("", response_model=AssembleEnqueued)
async def create_export(pid: str, body: ExportRequest) -> dict:
    return await enqueue_export(pid, body)


@router.get("/{export_id}/download")
def download_export(pid: str, export_id: int):
    require_project(pid)
    try:
        stream, file = current_context().exports.open_export(pid, export_id)
    except FileNotFoundError as error:
        raise ApiError(EXPORT_NOT_FOUND, 404, "Completed export not found") from error

    return ExportResponse(
        stream,
        media_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream",
        headers={
            "content-disposition": f"attachment; filename*=utf-8''{quote(file.name)}",
        },
    )
