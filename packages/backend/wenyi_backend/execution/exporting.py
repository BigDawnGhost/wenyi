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


class ExportPublisher(Protocol):
    def __call__(
        self, pid: str, export_id: int, output: str, *, job_id: int | None = None
    ) -> None: ...


@dataclass(frozen=True)
class ExportDependencies:
    jobs: JobRepository
    storage_for: Callable[[str], BackendStorage]
    render: Callable[[ExportRequest], str]
    publish: ExportPublisher
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
    pid, export_id = request.project_id, request.export_id
    with closing(dependencies.storage_for(pid)) as storage, storage.export_lock(export_id):
        job_id = None
        if request.run_id:
            job = _matching_job(request, dependencies)
            if job is None or job["status"] != "queued":
                return export_id
            job_id = job["id"]
            if not dependencies.jobs.set_job_status(job_id, "running", expected_status="queued"):
                return export_id
        try:
            dependencies.set_status(export_id, "running")
            output = dependencies.render(request)
            dependencies.publish(pid, export_id, output, job_id=job_id)
            return export_id
        except BaseException as error:
            # Native render panics arrive as BaseException subclasses; record the
            # failure states under the export lock before re-raising.
            _record_failure(export_id, job_id, dependencies, error)
            raise


def _matching_job(request: ExportRequest, dependencies: ExportDependencies) -> dict | None:
    job = dependencies.jobs.get_job_by_arq_id(request.run_id) if request.run_id else None
    if (
        job
        and job["kind"] == "export"
        and job["project_id"] == request.project_id
        and job["params"].get("export_id") == request.export_id
    ):
        return job
    return None


def _record_failure(
    export_id: int, job_id: int | None, dependencies: ExportDependencies, error: BaseException
) -> None:
    message = str(error)
    if job_id is not None:
        dependencies.jobs.set_job_status(
            job_id, "error", error=message, expected_status={"queued", "running"}
        )
    dependencies.set_status(export_id, "error", error=message)


def _record_startup_failure(
    request: ExportRequest, dependencies: ExportDependencies, error: BaseException
) -> None:
    job = _matching_job(request, dependencies)
    if not request.run_id or (job and job["status"] in {"queued", "running", "error"}):
        _record_failure(request.export_id, job["id"] if job else None, dependencies, error)


async def run(request: ExportRequest, dependencies: ExportDependencies) -> int:
    task = asyncio.create_task(asyncio.to_thread(execute, request, dependencies))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A snapshot render must finish publication before its worker slot is released.
        await asyncio.shield(task)
        raise
    except BaseException as error:
        # Also handle startup/lock failures before execute could record the error.
        await asyncio.to_thread(_record_startup_failure, request, dependencies, error)
        raise
