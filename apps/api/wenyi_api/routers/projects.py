"""Project creation, asynchronous source preview and resumable workflow jobs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import dal, paths
from ..config import settings
from ..job_service import start_job
from ..project_service import (
    config_document,
    effective_config,
    project_write,
    require_book,
    require_project,
    storage_for,
)
from ..schemas import (
    AssembleEnqueued,
    JobEnqueued,
    Message,
    Project,
    ProjectCreate,
    ProjectDetail,
    StartTranslation,
    UploadPreview,
)
from ..strategies import strategy_to_config

router = APIRouter(prefix="/projects", tags=["projects"])
_UPLOAD_FORMATS = {
    "epub": "epub",
    "fb2": "fb2",
    "txt": "text",
    "text": "text",
    "md": "markdown",
    "markdown": "markdown",
    "html": "html",
    "htm": "html",
    "pdf": "pdf",
    "docx": "docx",
    "srt": "srt",
}


@router.get("", response_model=list[Project])
def list_projects() -> list[dict]:
    return dal.list_projects()


@router.post("", response_model=Project, status_code=201)
def create_project(body: ProjectCreate) -> dict:
    from wenyi_core.config import Config

    try:
        if body.source_lang == body.target_lang:
            raise ValueError("Source and target languages are identical")
        strategy_to_config(
            body.strategy,
            Config.load(settings.config_path),
            source_lang=body.source_lang,
            target_lang=body.target_lang,
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    pid = dal.create_project(body.name, body.source_lang, body.target_lang, body.strategy)
    return require_project(pid)


@router.get("/{pid}", response_model=ProjectDetail)
def get_project(pid: str) -> dict:
    project = require_project(pid)
    summaries = dal.chapter_summaries(pid)
    project.update(
        chapter_count=len(summaries),
        total_word_count=dal.total_word_count(pid),
        done_chapters=sum(ch["status"] == "done" for ch in summaries),
    )
    return project


@router.post("/{pid}/upload", response_model=JobEnqueued)
async def upload_source(
    pid: str, file: UploadFile = File(...), fmt: str | None = Form(None)
) -> dict:
    with project_write(pid) as (project, storage):
        if project.get("initialized"):
            raise HTTPException(409, "Create a new project to replace an initialized source")
        filename = Path(file.filename or "source").name
        ext = (fmt or Path(filename).suffix.lstrip(".")).lower()
        if ext not in _UPLOAD_FORMATS:
            raise HTTPException(422, "Unsupported input format")
        fmt_code = _UPLOAD_FORMATS[ext]
        suffix = "md" if fmt_code == "markdown" else "txt" if fmt_code == "text" else fmt_code
        destination = Path(paths.project_dir(pid)) / f"source-{uuid4().hex}.{suffix}"
        digest = hashlib.sha256()
        try:
            with destination.open("xb") as handle:
                while block := await file.read(1024 * 1024):
                    digest.update(block)
                    handle.write(block)
            if destination.stat().st_size == 0:
                raise HTTPException(422, "Source file is empty")
            dal.set_project_source(
                pid,
                os.path.relpath(destination, settings.data_dir),
                Path(filename).stem,
                source_sha256=digest.hexdigest(),
                fmt=fmt_code,
                source_meta={"original_filename": filename},
            )
            storage.delete_artifact("preview.json")
            storage.delete_artifact("parsed_document.json")
            storage.delete_artifact("subtitle_preview.json")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return await start_job(pid, "parse")


@router.get("/{pid}/preview", response_model=UploadPreview)
def preview(pid: str) -> dict:
    project = require_project(pid)
    result = storage_for(pid).read_artifact("preview.json")
    if result is None:
        raise HTTPException(409, project.get("error") or "Source preview is not ready")
    return result


@router.post("/{pid}/translate", response_model=JobEnqueued)
async def start_translation(pid: str, body: StartTranslation | None = None) -> dict:
    if body and body.strategy is not None:
        with project_write(pid) as (project, _storage):
            try:
                config = strategy_to_config(
                    body.strategy,
                    effective_config(project),
                    source_lang=project["source_lang"],
                    target_lang=project["target_lang"],
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            dal.set_project_strategy(pid, body.strategy)
            dal.set_project_config(pid, config_document(config))
    project = require_project(pid)
    return await start_job(pid, "srt" if project.get("fmt") == "srt" else "translation")


@router.post("/{pid}/prepare", response_model=JobEnqueued)
async def prepare_only(pid: str) -> dict:
    require_book(require_project(pid))
    return await start_job(pid, "prepare")


@router.post("/{pid}/assemble", response_model=AssembleEnqueued)
async def assemble_output(pid: str) -> dict:
    from ..schemas import ExportRequest
    from .export import enqueue_export

    return await enqueue_export(pid, ExportRequest(), kind="assemble")


@router.post("/{pid}/pause", response_model=Message)
def pause(pid: str) -> dict:
    project = require_project(pid)
    if project["status"] not in dal.RUNNING_PROJECT_STATUSES:
        raise HTTPException(409, "Project has no active task to pause")
    dal.set_project_status(pid, "pausing")
    return {"message": "pausing"}


@router.post("/{pid}/resume", response_model=JobEnqueued)
async def resume(pid: str) -> dict:
    project = require_project(pid)
    if project["status"] not in {"paused", "error", "uploaded"}:
        raise HTTPException(409, "Project has no interrupted task")
    previous = dal.latest_resumable_job(pid)
    if previous is None:
        raise HTTPException(409, "Project has no resumable task")
    params = dict(previous.get("params") or {})
    return await start_job(pid, previous["kind"], params=params)


@router.delete("/{pid}", response_model=Message)
def delete_project(pid: str) -> dict:
    with project_write(pid) as (_project, storage):
        with storage.state_lock(), storage._conn as conn:
            active = conn.execute(
                "SELECT 1 FROM retranslation_requests WHERE project_id=%s AND status IN ('queued','running') LIMIT 1",
                (pid,),
            ).fetchone()
            if active:
                raise HTTPException(409, "请等待段落重译任务完成后再删除项目")
            conn.execute("DELETE FROM projects WHERE id=%s", (pid,))
    return {"message": "deleted"}
