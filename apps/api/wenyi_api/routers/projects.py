"""Project creation, asynchronous source preview and resumable workflow jobs."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import Json

from .. import dal
from ..config_documents import project_document
from ..global_settings import load_settings, registry_guard
from ..job_service import start_job
from ..project_service import (
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
from ..source_upload import input_format, save_source
from ..strategies import strategy_to_config

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[Project])
def list_projects() -> list[dict]:
    return dal.list_projects()


@router.post("", response_model=ProjectDetail, status_code=201)
async def create_project(
    project: Annotated[Json[ProjectCreate], Form()], file: UploadFile = File(...)
) -> dict:
    fmt = input_format(file)
    if fmt == "srt" and project.prepare:
        raise HTTPException(422, "Subtitles do not use book preparation")
    if project.source_lang == project.target_lang:
        raise HTTPException(422, "Source and target languages are identical")
    pid = uuid4().hex[:16]
    source = await save_source(pid, file)
    try:
        with registry_guard() as conn:
            defaults = load_settings(connection=conn)
            strategy = project.strategy or {"template": defaults.default_template}
            config = strategy_to_config(
                strategy,
                defaults.config,
                source_lang=project.source_lang,
                target_lang=project.target_lang,
            )
            if fmt == "pdf" and project.pdf_backend:
                config.pipeline.pdf_backend = project.pdf_backend
            dal.create_project(
                project.name,
                project.source_lang,
                project.target_lang,
                strategy,
                project_id=pid,
                source=source.fields(),
                config=project_document(config),
                connection=conn,
            )
    except ValueError as error:
        source.path.unlink(missing_ok=True)
        raise HTTPException(422, str(error)) from error
    except BaseException:
        source.path.unlink(missing_ok=True)
        raise
    try:
        await start_job(pid, "prepare" if project.prepare else "parse", params={"preview": True})
    except HTTPException as error:
        if error.status_code != 503:
            raise
        # The source and failed task are durable; return their identity for retry.
        dal.set_project_status(pid, "error", error=str(error.detail))
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
        source = await save_source(pid, file, fmt)
        try:
            dal.set_project_source(pid, **source.fields())
            storage.delete_artifact("preview.json")
            storage.delete_artifact("parsed_document.json")
            storage.delete_artifact("subtitle_preview.json")
        except BaseException:
            source.path.unlink(missing_ok=True)
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
        with project_write(pid) as (project, _storage), registry_guard() as conn:
            try:
                config = strategy_to_config(
                    body.strategy,
                    effective_config(project, defaults=load_settings(connection=conn).config),
                    source_lang=project["source_lang"],
                    target_lang=project["target_lang"],
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            dal.set_project_strategy(pid, body.strategy, connection=conn)
            dal.set_project_config(pid, project_document(config), connection=conn)
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
    with project_write(pid):
        from ..db import get_pool

        with get_pool().connection() as conn:
            conn.execute("DELETE FROM projects WHERE id=%s", (pid,))
    return {"message": "deleted"}
