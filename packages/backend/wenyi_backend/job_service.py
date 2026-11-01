"""Persist task identity before enqueueing and roll back failed queue submissions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from wenyi_core.config import Config

from .context import BackendStorage
from .ports.jobs import JobAdmission
from .ports.projects import ProjectReader
from .project_service import config_document, project_write

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


async def start_job(
    pid: str,
    kind: str,
    *,
    projects: ProjectReader,
    jobs: JobAdmission,
    storage_for: Callable[[str], BackendStorage],
    config_for: Callable[[dict], Config],
    enqueue: Callable[..., Awaitable[Any]],
    params: dict | None = None,
) -> dict:
    if kind not in TASK_FUNCTIONS:
        raise HTTPException(422, "unsupported task kind")
    params = dict(params or {})
    params.pop("config_snapshot", None)
    with project_write(pid, projects, storage_for) as (project, _storage):
        if not project.get("source_path"):
            raise HTTPException(409, "upload a source file first")
        run_id = uuid4().hex
        try:
            snapshot = config_document(config_for(project))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        db_id = jobs.admit_job(
            pid,
            kind,
            run_id,
            project_status=TASK_STATUSES[kind],
            params=params,
            run_id=run_id,
            config_snapshot=snapshot,
        )
        try:
            job = await enqueue(
                TASK_FUNCTIONS[kind], _job_id=run_id, project_id=pid, run_id=run_id, **params
            )
            if job is None:
                raise RuntimeError("The queue did not accept this task")
        except Exception as error:
            jobs.fail_admission(db_id, project["status"], str(error))
            raise HTTPException(503, "Task queue is unavailable; retry the operation") from error
    return {"job_id": run_id, "project_id": pid, "kind": kind}
