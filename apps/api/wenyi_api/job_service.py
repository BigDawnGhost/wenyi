"""Persist task identity before enqueueing and roll back failed queue submissions."""

from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from . import dal
from .project_service import config_document, effective_config, project_write
from .workers import enqueue

TASK_FUNCTIONS = {
    "parse": "run_parse",
    "prepare": "run_prepare",
    "translation": "run_translation",
    "chapter_translation": "run_chapter_translation",
    "review": "run_review",
    "srt": "run_srt",
}
TASK_STATUSES = {
    "parse": "parsing",
    "prepare": "preparing",
    "translation": "translating",
    "chapter_translation": "translating",
    "review": "reviewing",
    "srt": "translating",
}


async def start_job(pid: str, kind: str, *, params: dict | None = None) -> dict:
    if kind not in TASK_FUNCTIONS:
        raise HTTPException(422, "unsupported task kind")
    params = dict(params or {})
    params.pop("config_snapshot", None)
    with project_write(pid) as (project, _storage):
        if not project.get("source_path"):
            raise HTTPException(409, "upload a source file first")
        run_id = uuid4().hex
        try:
            snapshot = config_document(effective_config(project))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        db_id = dal.create_job(
            pid,
            kind,
            run_id,
            params=params,
            run_id=run_id,
            config_snapshot=snapshot,
            status="queued",
        )
        dal.set_project_status(pid, TASK_STATUSES[kind])
        try:
            job = await enqueue(
                TASK_FUNCTIONS[kind], _job_id=run_id, project_id=pid, run_id=run_id, **params
            )
            if job is None:
                raise RuntimeError("The queue did not accept this task")
        except Exception as error:
            dal.set_job_status(db_id, "error", error=str(error))
            dal.set_project_status(pid, project["status"])
            raise HTTPException(503, "Task queue is unavailable; retry the operation") from error
    return {"job_id": run_id, "project_id": pid, "kind": kind}
