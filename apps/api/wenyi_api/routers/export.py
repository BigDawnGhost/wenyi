"""Queue independent exports and serve authenticated, project-scoped downloads."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from wenyi_core.assemble.writer_common import default_output_format

from .. import dal
from ..config import settings
from ..db import get_pool
from ..project_service import config_document, effective_config, require_project, storage_for
from ..schemas import AssembleEnqueued, ExportOut, ExportRequest
from ..workers import enqueue

router = APIRouter(prefix="/projects/{pid}/exports", tags=["export"])


@router.get("", response_model=list[ExportOut])
def list_exports(pid: str) -> list[dict]:
    require_project(pid)
    with get_pool().connection() as connection:
        rows = connection.execute(
            "SELECT id,project_id,format,status,path,size,created_at,options,error FROM exports WHERE project_id=%s ORDER BY id DESC",
            (pid,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(
            zip(
                (
                    "id",
                    "project_id",
                    "format",
                    "status",
                    "path",
                    "size",
                    "created_at",
                    "options",
                    "error",
                ),
                row,
            )
        )
        item["created_at"] = item["created_at"].isoformat() if item["created_at"] else None
        result.append(item)
    return result


async def enqueue_export(pid: str, body: ExportRequest, *, kind: str = "export") -> dict:
    project = require_project(pid)
    if not project.get("initialized"):
        raise HTTPException(409, "Prepare or translate the source before exporting")
    store = storage_for(pid)
    manifest = store.load_manifest()
    fmt = body.format or (
        "srt"
        if project["fmt"] == "srt"
        else "docx"
        if project["fmt"] == "docx"
        else default_output_format(manifest)
    )
    if (project["fmt"] == "srt") != (fmt == "srt"):
        raise HTTPException(422, "Subtitle projects export SRT; book projects export book formats")
    options = body.model_dump(exclude={"format"})
    meta = manifest.get("meta") or {}
    is_babeldoc = meta.get("pdf_export") == "babeldoc" or bool(meta.get("babeldoc"))
    if fmt == "pdf" and not is_babeldoc:
        engines = [
            name
            for name, module in (("weasyprint", "weasyprint"), ("fpdf2", "fpdf"))
            if importlib.util.find_spec(module)
        ]
        if not engines:
            raise HTTPException(422, "PDF export requires the optional pdf-export dependencies")
        if "pdf_engine" not in body.model_fields_set:
            options["pdf_engine"] = engines[0]
        elif body.pdf_engine not in engines:
            raise HTTPException(422, f"PDF engine {body.pdf_engine} is not installed")
    snapshot = config_document(effective_config(project))
    export_id = dal.create_export(pid, fmt, options)
    run_id = uuid4().hex
    job_id = None
    try:
        job_id = dal.create_job(
            pid,
            "export",
            run_id,
            run_id=run_id,
            params={"export_id": export_id},
            config_snapshot=snapshot,
        )
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
        if job_id:
            dal.set_job_status(job_id, "error", error=str(error))
        dal.set_export_status(export_id, "error", error=str(error))
        raise HTTPException(503, "Export queue is unavailable; retry the operation") from error
    return {"job_id": run_id, "project_id": pid, "kind": kind, "export_id": export_id}


@router.post("", response_model=AssembleEnqueued)
async def create_export(pid: str, body: ExportRequest) -> dict:
    return await enqueue_export(pid, body)


@router.get("/{export_id}/download")
def download_export(pid: str, export_id: int):
    require_project(pid)
    with get_pool().connection() as connection:
        row = connection.execute(
            "SELECT path,status FROM exports WHERE id=%s AND project_id=%s", (export_id, pid)
        ).fetchone()
    if row is None or not row[0] or row[1] != "done":
        raise HTTPException(404, "Completed export not found")
    root = Path(settings.data_dir).resolve()
    file = (root / row[0]).resolve()
    if not file.is_relative_to(root / pid) or not file.is_file():
        raise HTTPException(404, "Export file missing")
    return FileResponse(str(file), filename=file.name)
