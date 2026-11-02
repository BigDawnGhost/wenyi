"""Book and source-preview work, without scheduler or task-state ownership."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from wenyi_core.config import Config
from wenyi_core.events import ProgressFn
from wenyi_core.llm.base import LLMClient

from ..context import BackendStorage
from ..ports.projects import ProjectRepository
from ..source_view import book_preview
from .contracts import ExecutionRequest


@dataclass(frozen=True)
class OperationServices:
    projects: ProjectRepository
    config_for: Callable[[str, str | None], Config]
    source_for: Callable[[str], str]
    build_client: Callable[[Config], LLMClient]
    exports_dir: Callable[[str], str]
    mineru_token_resolver: Callable[[], str | None] | None = None


def parse_source(
    pid: str,
    storage: BackendStorage,
    config: Config,
    progress: ProgressFn,
    services: OperationServices,
) -> str:
    from wenyi_core.pipeline.input_preparation import parse_document

    source = services.source_for(pid)
    project = services.projects.get_project(pid)
    if project is None:
        raise ValueError(f"Project not found: {pid}")
    progress(0, 1, "解析原文")
    doc = parse_document(
        storage, source, config, mineru_token_resolver=services.mineru_token_resolver
    )
    preview = book_preview(doc, project["fmt"])
    storage.write_artifact("preview.json", preview)
    progress(1, 1, "原文预览已就绪")
    return "uploaded"


def book_operation(
    request: ExecutionRequest,
    storage: BackendStorage,
    config: Config,
    client: LLMClient,
    progress: ProgressFn,
    services: OperationServices,
) -> str:
    from wenyi_core.pipeline.orchestrator import Orchestrator

    if request.params.get("autofix") is not None:
        config.pipeline.review_autofix = request.params["autofix"]
    orch = Orchestrator(
        config,
        client=client,
        storage=storage,
        mineru_token_resolver=services.mineru_token_resolver,
    )
    source = services.source_for(request.project_id)
    if request.kind == "prepare":
        orch.prepare_for_translation(source, progress=progress)
        return "prepared"
    if request.kind == "chapter_translation":
        orch.run(source, only_chapter=request.params["chapter_index"], progress=progress)
        return "done" if not storage.pending_chapters() else "prepared"
    if request.kind == "review":
        orch.run_review(source, progress=progress)
        return "reviewed"
    steps = {"translate"}
    if config.pipeline.review:
        steps.add("review")
    orch.run_steps(source, steps, progress=progress)
    return "done"


def run_operation(
    request: ExecutionRequest,
    storage: BackendStorage,
    progress: ProgressFn,
    bind_client: Callable[[LLMClient], None],
    *,
    services: OperationServices,
) -> str:
    pid, kind = request.project_id, request.kind
    config = services.config_for(pid, request.run_id)
    if kind == "parse":
        return parse_source(pid, storage, config, progress, services)
    if kind == "prepare" and not storage.exists():
        # Source inspection survives missing credentials or failed AI preparation.
        parse_source(pid, storage, config, progress, services)
    client = services.build_client(config)
    bind_client(client)
    with client.interrupt_scope():
        from wenyi_core.llm.operations import configured_operations

        if request.params.get("autofix") is not None:
            config.pipeline.review_autofix = request.params["autofix"]
        client.validate_credentials(
            configured_operations(
                config,
                "review" if kind == "review" else "prepare" if kind == "prepare" else "translate",
            )
        )
        return book_operation(request, storage, config, client, progress, services)
