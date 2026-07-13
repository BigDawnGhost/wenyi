"""导出：发起导出任务 / 列表 / 下载。"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import dal
from ..config import settings
from ..db import get_pool
from ..schemas import ExportOut, ExportRequest, JobEnqueued
from ..workers import enqueue

router = APIRouter(prefix="/projects/{pid}/exports", tags=["export"])


@router.get("", response_model=list[ExportOut])
def list_exports(pid: str) -> list[dict]:
    with get_pool().connection() as c:
        rows = c.execute(
            """SELECT id, project_id, format, status, path, size, created_at
               FROM exports WHERE project_id=%s ORDER BY created_at DESC""",
            (pid,),
        ).fetchall()
    return [
        {"id": r[0], "project_id": r[1], "format": r[2], "status": r[3],
         "path": r[4], "size": r[5],
         "created_at": r[6].isoformat() if r[6] else None}
        for r in rows
    ]


@router.post("", response_model=JobEnqueued)
async def create_export(pid: str, body: ExportRequest) -> dict:
    if dal.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    export_id = dal.create_export(
        pid,
        body.format,
        {
            "bilingual": body.bilingual,
            "order": body.order,
            "about_page": body.about_page,
        },
    )
    try:
        job = await enqueue(
            "run_export",
            project_id=pid,
            export_id=export_id,
            fmt=body.format,
            bilingual=body.bilingual,
            order=body.order,
            about_page=body.about_page,
        )
    except Exception:
        dal.set_export_status(export_id, "error")
        raise
    return {"job_id": job.job_id if job else "sync", "project_id": pid, "kind": "export"}


@router.get("/{export_id}/download")
def download_export(pid: str, export_id: int):
    with get_pool().connection() as c:
        r = c.execute(
            "SELECT path, format FROM exports WHERE id=%s AND project_id=%s",
            (export_id, pid),
        ).fetchone()
    if r is None or not r[0]:
        raise HTTPException(404, "export not found")
    rel_path = r[0]
    abs_path = rel_path if os.path.isabs(rel_path) else os.path.join(
        settings.data_dir, rel_path)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, "file missing")
    return FileResponse(abs_path, filename=os.path.basename(abs_path))
