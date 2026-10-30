"""Queue independent exports and serve authenticated, project-scoped downloads."""

from __future__ import annotations

import mimetypes
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from ..context import current_context
from ..export_plan import resolve_export
from ..export_response import ExportResponse
from ..project_service import config_document, effective_config, require_project, storage_for
from ..schemas import AssembleEnqueued, ExportOut, ExportRequest
from ..workers import enqueue

router = APIRouter(prefix="/projects/{pid}/exports", tags=["export"])


@router.get("", response_model=list[ExportOut])
def list_exports(pid: str) -> list[dict]:
    require_project(pid)
    return current_context().exports.list_exports(pid)


async def enqueue_export(pid: str, body: ExportRequest, *, kind: str = "export") -> dict:
    project = require_project(pid)
    if not project.get("initialized"):
        raise HTTPException(409, "Prepare or translate the source before exporting")
    store = storage_for(pid)
    try:
        fmt, options = resolve_export(project, store.load_manifest(), body)
    finally:
        store.close()
    snapshot = config_document(effective_config(project))
    run_id = uuid4().hex
    exports = current_context().exports
    try:
        export_id, job_id = exports.admit_export(pid, fmt, options, run_id, snapshot)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "Export queue is unavailable; retry the operation") from error
    try:
        job = await enqueue(
            "run_export",
            _job_id=run_id,
            project_id=pid,
            run_id=run_id,
            export_id=export_id,
            fmt=fmt,
            **options,
        )
        if job is None:
            raise RuntimeError("The queue did not accept this export")
    except Exception as error:
        exports.fail_export_admission(job_id, str(error))
        raise HTTPException(503, "Export queue is unavailable; retry the operation") from error
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
        raise HTTPException(404, "Completed export not found") from error

    return ExportResponse(
        stream,
        media_type=mimetypes.guess_type(file.name)[0] or "application/octet-stream",
        headers={
            "content-disposition": f"attachment; filename*=utf-8''{quote(file.name)}",
        },
    )
