"""Stable scheduler entry points; execution services own domain work and lifecycle."""

from __future__ import annotations

from functools import wraps

from wenyi_core.pdf_bridge.connection import use_bridge_connection

from ..context import current_context, use_context
from ..execution import exporting, runner
from ..execution.bindings import export_dependencies, workflow_dependencies
from ..execution.contracts import ExecutionRequest, ExportRequest, WorkflowKind


def _worker_entry(function):
    @wraps(function)
    async def run(ctx, **kwargs):
        with (
            use_context(ctx["backend"]),
            use_bridge_connection(ctx["backend"].babeldoc_connection),
        ):
            return await function(ctx, **kwargs)

    return run


async def _run(kind: WorkflowKind, project_id: str, run_id: str | None, params: dict) -> None:
    await runner.run(
        ExecutionRequest(kind, project_id, run_id, params),
        workflow_dependencies(current_context()),
    )


@_worker_entry
async def run_parse(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("parse", project_id, run_id, params)


@_worker_entry
async def run_prepare(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("prepare", project_id, run_id, params)


@_worker_entry
async def run_translation(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("translation", project_id, run_id, params)


@_worker_entry
async def run_chapter_translation(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("chapter_translation", project_id, run_id, params)


@_worker_entry
async def run_review(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("review", project_id, run_id, params)


@_worker_entry
async def run_srt(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("srt", project_id, run_id, params)


@_worker_entry
async def run_export(
    ctx, *, project_id: str, export_id: int, run_id: str | None = None, **params
) -> int:
    return await exporting.run(
        ExportRequest(project_id, export_id, run_id, params),
        export_dependencies(current_context()),
    )
