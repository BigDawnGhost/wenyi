"""Recover durable workflow/export rows after a worker or Redis queue disappears."""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from arq.jobs import Job, JobStatus
from wenyi_backend.context import BackendStorage, current_context
from wenyi_backend.execution.runner import WorkflowOutcome, finish_catalog_job

from .. import dal
from ..db import get_pool
from ..export_retention import recover_export_cleanup

_REMOTE_ACTIVE = {JobStatus.queued, JobStatus.deferred, JobStatus.in_progress}


@runtime_checkable
class RecoveryStorage(BackendStorage, Protocol):
    """Session-lock capabilities shared by PostgreSQL and local SQLite recovery."""


async def recover_jobs(ctx: dict) -> None:
    # The grace period avoids the normal interval between SQL creation and enqueue.
    with get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT id,project_id,arq_job_id,kind,params,updated_at FROM jobs
            WHERE status IN ('queued','running')
            AND updated_at < now() - interval '2 minutes' ORDER BY id"""
        ).fetchall()
    for job_id, pid, arq_id, kind, params, updated_at in rows:
        is_export = kind == "export"
        export_id = (params or {}).get("export_id") if is_export else None
        if is_export and (
            not isinstance(export_id, int) or isinstance(export_id, bool) or export_id <= 0
        ):
            dal.set_job_status(
                job_id, "error", error="Export task is missing its persisted export identifier"
            )
            continue
        export_id_i: int | None = None
        if is_export:
            # Validated above: export_id is a positive int.
            assert isinstance(export_id, int), f"expected int export_id, got {export_id!r}"
            export_id_i = export_id
        storage = current_context().storage_for(pid)
        assert isinstance(storage, RecoveryStorage), "recovery worker requires RecoveryStorage"
        lock = (
            storage.export_lock(export_id_i, blocking=False)
            if export_id_i is not None
            else storage.lock(blocking=False)
        )
        try:
            with lock:
                # Mutual exclusion makes inspection safe. Contention says nothing about
                # writer progress; never take over a live writer on a telemetry timeout.
                current = dal.get_job(job_id)
                if (
                    not current
                    or current["status"] not in {"queued", "running"}
                    or current["updated_at"] != updated_at.isoformat()
                ):
                    continue
                if current["status"] == "queued":
                    queue = "wenyi:exports" if is_export else "wenyi:workflows"
                    remote = await Job(arq_id, ctx["redis"], _queue_name=queue).status()
                    if remote in _REMOTE_ACTIVE:
                        continue
                if is_export:
                    message = (
                        "Export worker stopped before completion; create a new export to retry"
                    )
                    assert export_id_i is not None, "export error path lost its export id"
                    dal.set_job_status(job_id, "error", error=message)
                    dal.set_export_status(export_id_i, "error", error=message)
                    storage.log_event(
                        "export_interrupted", run_id=arq_id, export_id=export_id_i, error=message
                    )
                else:
                    message = "Worker stopped before completion; resume this task to continue from saved progress"
                    orphan = WorkflowOutcome("interrupted", "paused", RuntimeError(message))
                    if finish_catalog_job(current_context().repository, current, orphan) is orphan:
                        storage.log_event("task_interrupted", run_id=arq_id, error=message)
        except BlockingIOError:
            continue
        finally:
            storage.close()
    await asyncio.to_thread(recover_export_cleanup, get_pool(), current_context().data_dir)
