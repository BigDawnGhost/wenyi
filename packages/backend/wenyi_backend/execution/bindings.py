"""Composition boundary from an application's context to explicit execution dependencies."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path

from wenyi_core.config import Config
from wenyi_core.llm.base import LLMClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.protocol import Storage

from ..config_documents import config_from_document
from ..context import BackendContext
from ..project_service import effective_config
from .contracts import ExecutionDependencies, ExportRequest
from .exporting import ExportDependencies, render_export
from .operations import OperationServices, run_operation
from .ports import WorkflowRunner


def build_workflow(
    config: Config,
    client: LLMClient,
    storage: Storage,
    *,
    mineru_token_resolver: Callable[[], str | None] | None = None,
) -> WorkflowRunner:
    """Bind one workflow runner; the only module that names the orchestration class."""
    return Orchestrator(
        config,
        client=client,
        storage=storage,
        mineru_token_resolver=mineru_token_resolver,
    )


def resolve_source(context: BackendContext, pid: str) -> str:
    project = context.repository.get_project(pid)
    if project is None:
        raise ValueError(f"Project not found: {pid}")
    if not project.get("source_path"):
        raise ValueError("Project has no uploaded source")
    if context.source_paths is not None:
        source = context.source_paths.source_path(pid, project["source_path"])
    else:
        source = Path(project["source_path"])
        if not source.is_absolute():
            source = Path(context.data_dir) / source
    if not source.is_file():
        raise ValueError("Uploaded source file is missing")
    return str(source)


def build_config_for(context: BackendContext, pid: str, run_id: str | None = None) -> Config:
    project = context.repository.get_project(pid)
    if project is None:
        raise ValueError("Project does not exist")
    job = context.repository.get_job_by_arq_id(run_id) if run_id else None
    snapshot = ((job or {}).get("params") or {}).get("config_snapshot")
    state_dir = str(context.project_dir(pid))
    if snapshot:
        return config_from_document({**snapshot, "paths": {"state_dir": state_dir}})
    return effective_config(
        project, defaults=context.settings_store.load().config, state_dir=state_dir
    )


def operation_services(context: BackendContext) -> OperationServices:
    def exports_dir(pid: str) -> str:
        directory = Path(context.project_dir(pid)) / "exports"
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    return OperationServices(
        projects=context.repository,
        config_for=partial(build_config_for, context),
        source_for=partial(resolve_source, context),
        build_client=context.build_client,
        exports_dir=exports_dir,
        build_workflow=partial(build_workflow, mineru_token_resolver=context.mineru_token_resolver),
        mineru_token_resolver=context.mineru_token_resolver,
    )


def workflow_dependencies(context: BackendContext) -> ExecutionDependencies:
    return ExecutionDependencies(
        projects=context.repository,
        jobs=context.repository,
        storage_for=context.storage_for,
        telemetry=context.telemetry,
        operation=partial(run_operation, services=operation_services(context)),
        run_sync=context.run_workflow_sync,
        metrics=context.metrics,
    )


def export_dependencies(context: BackendContext) -> ExportDependencies:
    services = operation_services(context)

    def render(request: ExportRequest) -> str:
        options = dict(request.options)
        # Previously queued jobs may carry the retired local PDF renderer option.
        options.pop("pdf_engine", None)
        return render_export(
            request.project_id,
            export_id=request.export_id,
            run_id=request.run_id,
            services=services,
            storage_for=context.storage_for,
            **options,
        )

    return ExportDependencies(
        jobs=context.repository,
        storage_for=context.storage_for,
        render=render,
        publish=context.exports.publish_export,
        set_status=context.repository.set_export_status,
    )
