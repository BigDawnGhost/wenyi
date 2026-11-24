"""Queue independent exports and serve authenticated, project-scoped downloads."""

from __future__ import annotations

import asyncio
import mimetypes
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


def _resolve_input(project: dict, body: ExportRequest) -> tuple[str, dict]:
    store = storage_for(project["id"])
    try:
        return resolve_export(project, export_input(project, store, effective_config), body)
    finally:
        store.close()


async def enqueue_export(pid: str, body: ExportRequest, *, kind: str = "export") -> dict:
    project = require_project(pid)
    # Loading an uninitialized book's saved parse can be expensive. Keep the HTTP
    # event loop free and open/close the adapter on the same worker thread.
    fmt, options = await asyncio.to_thread(_resolve_input, project, body)
    try:
        snapshot = runtime_document(effective_config(project))
    except ValueError as error:
        # Corrupt or legacy-saved project config must read as invalid_configuration like
        # the config endpoints, not as a bare 500 (audit Y-4/E-3).
        raise ApiError(INVALID_CONFIGURATION, 422, str(error)) from None
    run_id = uuid4().hex
    exports = current_context().exports
    try:
        export_id, job_id = exports.admit_export(pid, fmt, options, run_id, snapshot)
    except (ApiError, StarletteHTTPException):
        # admit_export already carries a code; re-raise it untouched instead of
        # letting the generic handler relabel it as a queue failure.
        raise
    except Exception as error:
        raise ApiError(
            QUEUE_UNAVAILABLE, 503, "Export queue is unavailable; retry the operation"
        ) from error
    admitted = current_context().repository.get_job(job_id)
    admitted_run_id = admitted.get("run_id") if isinstance(admitted, dict) else None
    if admitted_run_id and admitted_run_id != run_id:
        # An identical in-flight export already owns this admission slot; reuse it
        # instead of dispatching a duplicate queue task.
        return {
            "job_id": admitted_run_id,
            "project_id": pid,
            "kind": kind,
            "export_id": export_id,
        }
    try:
        job = await enqueue(
            "run_export",
            _job_id=run_id,
            project_id=pid,
            run_id=run_id,
            export_id=export_id,
            fmt=fmt,
            **(
                {"parsed_source_sha256": project["source_sha256"]}
                if not project.get("initialized")
                else {}
            ),
            **options,
        )
        if job is None:
            raise RuntimeError("The queue did not accept this export")
    except Exception as error:
        exports.fail_export_admission(job_id, str(error))
        raise ApiError(
            QUEUE_UNAVAILABLE, 503, "Export queue is unavailable; retry the operation"
        ) from error
    return {"job_id": run_id, "project_id": pid, "kind": kind, "export_id": export_id}


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
