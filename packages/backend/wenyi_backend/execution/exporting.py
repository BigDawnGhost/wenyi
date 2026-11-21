"""Render snapshots separately from locked export claim, publication and terminal state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..context import BackendStorage
from ..ports.jobs import JobRepository
from .contracts import ExportRequest
from .operations import OperationServices


class ExportStatusWriter(Protocol):
    def __call__(self, export_id: int, status: str, *, error: str | None = None) -> None: ...


@dataclass(frozen=True)
class ExportDependencies:
    jobs: JobRepository
    storage_for: Callable[[str], BackendStorage]
    render: Callable[[ExportRequest], str]
    publish: Callable[[str, int, str], object]
    set_status: ExportStatusWriter


def render_export(
    pid: str,
    *,
    export_id: int,
    fmt: str,
    services: OperationServices,
    storage_for: Callable[[str], BackendStorage],
    run_id: str | None = None,
    parsed_source_sha256: str | None = None,
    bilingual: bool = False,
    order: Literal["target_first", "source_first"] = "target_first",
    about_page: bool = True,
    preserve_source_style: bool = False,
) -> str:
    """Return a complete private output; only the coordinator may publish it."""
    from wenyi_core.assemble.policy import export_plan
    from wenyi_core.assemble.writer import assemble
    from wenyi_core.pipeline.prepare.input_preparation import load_parsed_document
    from wenyi_core.storage.language_policies import persist_plan
    from wenyi_core.storage.run_store import ExportSnapshotStore, source_sha256

    from ..export_plan import export_filename

    with closing(storage_for(pid)) as storage:
        source = services.source_for(pid)
        source_digest = source_sha256(source)
        config = services.config_for(pid, run_id)
        project = services.projects.get_project(pid)
        if project is None:
            raise ValueError(f"Project not found: {pid}")
        out_dir = Path(services.exports_dir(pid)) / str(export_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / export_filename(project, config.target_lang, fmt, bilingual))
        if parsed_source_sha256 is None:
            snapshot = storage.create_export_snapshot(actual_sha256=source_digest)
        else:
            if source_digest != parsed_source_sha256:
                raise ValueError("Source changed after export was requested")
            document = load_parsed_document(storage, source, config, actual_sha256=source_digest)
            if document is None:
                raise ValueError("The matching parsed source is no longer available; parse again")
            snapshot = ExportSnapshotStore.from_parsed_document(
                storage.run_dir, document, source_digest
            )
            del document
        with storage.assemble_lock():
            plan = export_plan(
                snapshot,
                fmt,
                bilingual=bilingual,
                order=order,
                preserve_source_style=preserve_source_style,
                about_page=about_page,
            )
            persist_plan(storage, plan)
            assemble(
                snapshot,
                source,
                out_path=out_path,
                out_format=fmt,
                bilingual=bilingual,
                order=order,
                about_page=about_page,
                preserve_source_style=preserve_source_style,
                babeldoc_timeout=config.pipeline.babeldoc_timeout,
                language_policy=plan,
            )
        if source_sha256(source) != source_digest:
            raise ValueError("Source changed during export; ensure the file is stable and retry")
        return out_path


def execute(request: ExportRequest, dependencies: ExportDependencies) -> int:
    pid, export_id, run_id = request.project_id, request.export_id, request.run_id
    job = None
    with closing(dependencies.storage_for(pid)) as storage, storage.export_lock(export_id):
        if run_id:
            job = dependencies.jobs.get_job_by_arq_id(run_id)
            if (
                not job
                or job["status"] != "queued"
                or job["kind"] != "export"
                or job["project_id"] != pid
                or job["params"].get("export_id") != export_id
            ):
                return export_id
            dependencies.jobs.set_job_status(job["id"], "running")
        dependencies.set_status(export_id, "running")
        try:
            output = dependencies.render(request)
            dependencies.publish(pid, export_id, output)
            if job:
                dependencies.jobs.set_job_status(job["id"], "done")
            return export_id
        except Exception as error:
            if job:
                dependencies.jobs.set_job_status(job["id"], "error", error=str(error))
            dependencies.set_status(export_id, "error", error=str(error))
            raise


def _record_failure(
    request: ExportRequest, dependencies: ExportDependencies, error: Exception
) -> None:
    dependencies.set_status(request.export_id, "error", error=str(error))
    job = dependencies.jobs.get_job_by_arq_id(request.run_id) if request.run_id else None
    if job and job["project_id"] == request.project_id:
        dependencies.jobs.set_job_status(job["id"], "error", error=str(error))


async def run(request: ExportRequest, dependencies: ExportDependencies) -> int:
    task = asyncio.create_task(asyncio.to_thread(execute, request, dependencies))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A snapshot render must finish publication before its worker slot is released.
        await asyncio.shield(task)
        raise
    except Exception as error:
        await asyncio.to_thread(_record_failure, request, dependencies, error)
        raise
