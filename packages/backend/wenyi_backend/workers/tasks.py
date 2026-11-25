"""Stable scheduler entry points; execution services own domain work and lifecycle."""

from __future__ import annotations

import asyncio
from functools import wraps

from wenyi_core.pdf_bridge.connection import use_bridge_connection

from .. import job_service
from ..context import BackendContext, current_context, use_context
from ..execution import exporting, runner
from ..execution.bindings import export_dependencies, workflow_dependencies
from ..execution.contracts import ExecutionRequest, ExportRequest, WorkflowKind
from ..execution.outage import AUTO_RESUME_ATTEMPT_PARAM
from ..project_service import effective_config
from .signing import SIGNATURE_FIELD, verify_task


def _worker_entry(function):
    @wraps(function)
    async def run(ctx, **kwargs):
        signature = kwargs.pop(SIGNATURE_FIELD, None)
        secret = getattr(ctx.get("backend"), "api_token", None)
        if not verify_task(function.__name__, kwargs, signature, secret):
            raise ValueError(f"Rejecting unsigned or tampered {function.__name__} payload")
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
async def run_export(
    ctx, *, project_id: str, export_id: int, run_id: str | None = None, **params
) -> int:
    return await exporting.run(
        ExportRequest(project_id, export_id, run_id, params),
        export_dependencies(current_context()),
    )


def _auto_resume_still_pending(
    context: BackendContext, project_id: str, expected_run_id: str | None
) -> bool:
    if not expected_run_id:
        return True
    cache = None
    try:
        cache = context.telemetry.connect()
        marker = cache.get(f"project:{project_id}:auto_resume")
    except Exception:
        return True
    finally:
        if cache is not None:
            try:
                context.telemetry.release(cache)
            except Exception:
                pass
    if marker is None:
        return True
    return marker == expected_run_id


@_worker_entry
async def run_auto_resume(
    ctx,
    *,
    project_id: str,
    expected_run_id: str | None = None,
    attempt: int = 1,
) -> bool:
    """Resume a paused workflow after a provider outage if it was not superseded or cancelled."""
    context = current_context()
    project = await asyncio.to_thread(context.repository.get_project, project_id)
    if project is None or project.get("status") != "paused":
        return False
    latest = await asyncio.to_thread(context.repository.latest_resumable_job, project_id)
    if latest is None or latest.get("status") != "paused":
        return False
    if expected_run_id and latest.get("run_id") != expected_run_id:
        return False
    if not await asyncio.to_thread(
        _auto_resume_still_pending, context, project_id, expected_run_id
    ):
        return False
    params = dict(latest.get("params") or {})
    params[AUTO_RESUME_ATTEMPT_PARAM] = max(1, int(attempt))
    await job_service.start_job(
        project_id,
        latest["kind"],
        projects=context.repository,
        jobs=context.repository,
        storage_for=context.storage_for,
        config_for=lambda proj: effective_config(
            proj,
            defaults=context.settings_store.load().config,
            state_dir=context.project_dir(proj["id"]),
        ),
        enqueue=context.enqueue,
        params=params,
    )
    return True
