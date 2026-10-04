"""Local-only native save plans and self-contained export transport."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from wenyi_backend.context import current_context
from wenyi_backend.export_plan import export_filename, resolve_export
from wenyi_backend.export_response import ExportResponse
from wenyi_backend.project_service import effective_config, require_project
from wenyi_backend.schemas import ExportRequest

from ..local_backend import LocalBackend

router = APIRouter(prefix="/desktop/projects/{pid}/exports", tags=["desktop-export"])


class NativeExportPlan(BaseModel):
    filename: str
    extension: str
    label: str
    request: ExportRequest | None
    protected_roots: list[str]


def _backend() -> LocalBackend:
    return current_context().repository


def _plan(filename: str, fmt: str, request: ExportRequest | None = None) -> NativeExportPlan:
    return NativeExportPlan(
        filename=filename + ".zip" if fmt == "html" else filename,
        extension="zip" if fmt == "html" else Path(filename).suffix.lstrip("."),
        label="HTML + assets (ZIP)" if fmt == "html" else fmt.upper(),
        request=request,
        protected_roots=[str(_backend().workspace.resolve())],
    )


@router.post("/plan")
def plan_export(pid: str, body: ExportRequest) -> NativeExportPlan:
    _backend()
    project = require_project(pid)
    if not project.get("initialized"):
        raise HTTPException(409, "Prepare or translate the source before exporting")
    store = _backend().storage_for(pid, create=False)
    try:
        fmt, options = resolve_export(project, store.load_manifest(), body)
    finally:
        store.close()
    filename = export_filename(
        project, effective_config(project).target_lang, fmt, options["bilingual"]
    )
    return _plan(filename, fmt, ExportRequest(format=fmt, **options))


@router.get("/{export_id}/plan")
def plan_history(pid: str, export_id: int) -> NativeExportPlan:
    backend = _backend()
    require_project(pid)
    try:
        stream, file = backend.open_export(pid, export_id)
    except FileNotFoundError as error:
        raise HTTPException(404, "Completed export not found") from error
    with stream:
        return _plan(file.name, file.suffix.lstrip("."))


@router.get("/{export_id}/content")
def save_content(pid: str, export_id: int) -> ExportResponse:
    backend = _backend()
    require_project(pid)
    try:
        stream, _ = backend.open_export(pid, export_id, bundle_html=True)
    except FileNotFoundError as error:
        raise HTTPException(404, "Completed export not found") from error
    except (OSError, ValueError) as error:
        raise HTTPException(409, "Export assets changed or contain unsafe paths") from error

    return ExportResponse(stream)
