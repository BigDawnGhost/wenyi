"""Recover durable workflow/export rows after a worker or Redis queue disappears."""

from __future__ import annotations

from arq.jobs import Job, JobStatus

from .. import dal
from ..db import get_pool
from ..project_service import storage_for

_REMOTE_ACTIVE = {JobStatus.queued, JobStatus.deferred, JobStatus.in_progress}


async def recover_jobs(ctx: dict) -> None:
    # The grace period avoids the normal interval between SQL creation and enqueue.
    with get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT id,project_id,arq_job_id,kind,params FROM jobs
            WHERE status IN ('queued','running')
            AND updated_at < now() - interval '2 minutes' ORDER BY id"""
        ).fetchall()
    for job_id, pid, arq_id, kind, params in rows:
        storage = storage_for(pid)
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
            assert isinstance(export_id, int)
            export_id_i = export_id
        lock = (
            storage.export_lock(export_id_i, blocking=False)
            if export_id_i is not None
            else storage.lock(blocking=False)
        )
        try:
            with lock:
                # Running workers keep these session locks through rendering/model calls.
                # No lock means that worker is gone even if Redis retains an in-progress TTL.
                current = dal.get_job(job_id)
                if not current or current["status"] not in {"queued", "running"}:
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
                    assert export_id_i is not None
                    dal.set_job_status(job_id, "error", error=message)
                    dal.set_export_status(export_id_i, "error", error=message)
                    storage.log_event(
                        "export_interrupted", run_id=arq_id, export_id=export_id_i, error=message
                    )
                else:
                    message = "Worker stopped before completion; resume this task to continue from saved progress"
                    dal.set_job_status(job_id, "interrupted", error=message)
                    latest = dal.list_jobs(pid)
                    workflow = next((job for job in latest if job["kind"] != "export"), None)
                    if workflow and workflow["id"] == job_id:
                        dal.set_project_status(pid, "paused", error=message)
                        storage.log_event("task_interrupted", run_id=arq_id, error=message)
        except BlockingIOError:
            continue
