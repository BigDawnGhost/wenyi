"""Project creation, asynchronous source preview and resumable workflow jobs."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import Json
from wenyi_core.assemble.writer_common import output_formats

from ..config_documents import project_document
from ..context import current_context
from ..global_settings import load_settings, registry_guard
from ..project_service import require_book, validate_translation_mode_for_format
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
from ..source_view import book_preview
from ..strategies import strategy_to_config
from .dependencies import (
    chapter_summaries,
    effective_config,
    parsed_source,
    project_write,
    read_storage_for,
    require_project,
    start_job,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[Project])
def list_projects() -> list[dict]:
    return current_context().repository.list_projects()


@router.post("", response_model=ProjectDetail, status_code=201)
async def create_project(
    project: Annotated[Json[ProjectCreate], Form()], file: UploadFile = File(...)
) -> dict:
    fmt = input_format(file)
    if fmt == "srt" and project.prepare:
        raise HTTPException(422, "Subtitles do not use book preparation")
    try:
        validate_translation_mode_for_format(project.translation_mode, fmt)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
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
                translation_mode=project.translation_mode,
            )
            if fmt == "pdf" and project.pdf_backend:
                config.pipeline.pdf_backend = project.pdf_backend
            current_context().repository.create_project(
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
        current_context().repository.set_project_status(pid, "error", error=str(error.detail))
    return require_project(pid)


@router.get("/{pid}", response_model=ProjectDetail)
def get_project(pid: str) -> dict:
    project = require_project(pid)
    summaries = chapter_summaries(project)
    manifest = {}
    if project.get("initialized") and project.get("fmt") != "srt":
        storage = read_storage_for(pid)
        try:
            manifest = storage.load_manifest()
        finally:
            storage.close()
    project.update(
        chapter_count=len(summaries),
        total_word_count=sum(ch["word_count"] for ch in summaries),
        done_chapters=sum(ch["status"] == "done" for ch in summaries),
        output_formats=["srt"] if project.get("fmt") == "srt" else output_formats(manifest),
    )
    return project


@router.post("/{pid}/upload", response_model=JobEnqueued)
async def upload_source(
    pid: str, file: UploadFile = File(...), fmt: str | None = Form(None)
) -> dict:
    with project_write(pid) as (project, storage):
        if project.get("initialized"):
            raise HTTPException(409, "Create a new project to replace an initialized source")
        try:
            effective_config({**project, "fmt": input_format(file, fmt)})
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        source = await save_source(pid, file, fmt)
        try:
            with storage.state_lock():
                storage.delete_artifact("preview.json")
                storage.delete_artifact("parsed_document.json")
                storage.delete_artifact("subtitle_preview.json")
        except BaseException:
            source.path.unlink(missing_ok=True)
            raise
        # Publish only after invalidation commits. Once publication is attempted,
        # its acknowledgement can be lost after the catalog commit; retain the
        # complete candidate rather than risk deleting a referenced original.
        current_context().repository.set_project_source(pid, **source.fields())
    return await start_job(pid, "parse")


@router.get("/{pid}/preview", response_model=UploadPreview)
def preview(pid: str) -> dict:
    project = require_project(pid)
    storage = read_storage_for(pid)
    try:
        if project.get("fmt") != "srt" and not project.get("initialized"):
            document = parsed_source(project, storage)
            result = book_preview(document, project["fmt"]) if document is not None else None
        else:
            result = storage.read_artifact("preview.json")
    finally:
        storage.close()
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
            repository = current_context().repository
            repository.set_project_strategy(pid, body.strategy, connection=conn)
            repository.set_project_config(pid, project_document(config), connection=conn)
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
    require_project(pid)
    repository = current_context().repository
    job = next((row for row in repository.list_jobs(pid) if row["kind"] != "export"), None)
    if job is None or not repository.request_pause(pid, job["run_id"]):
        raise HTTPException(409, "Project has no active task to pause")
    return {"message": "pausing"}


@router.post("/{pid}/resume", response_model=JobEnqueued)
async def resume(pid: str) -> dict:
    project = require_project(pid)
    if project["status"] not in {"paused", "error", "uploaded"}:
        raise HTTPException(409, "Project has no interrupted task")
    previous = current_context().repository.latest_resumable_job(pid)
    if previous is None:
        raise HTTPException(409, "Project has no resumable task")
    params = dict(previous.get("params") or {})
    return await start_job(pid, previous["kind"], params=params)


@router.delete("/{pid}", response_model=Message)
def delete_project(pid: str) -> dict:
    current_context().remove_project(pid)
    return {"message": "deleted"}
