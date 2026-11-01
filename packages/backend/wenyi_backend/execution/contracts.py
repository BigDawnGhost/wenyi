"""Typed requests and the capabilities owned by one workflow invocation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from wenyi_core.events import ProgressFn
from wenyi_core.llm.base import LLMClient

from ..context import BackendStorage, Telemetry, WorkflowDispatcher
from ..metrics import Metrics
from ..ports.jobs import JobRepository
from ..ports.projects import ProjectRepository

WorkflowKind = Literal["parse", "prepare", "translation", "chapter_translation", "review", "srt"]


@dataclass(frozen=True)
class ExecutionRequest:
    kind: WorkflowKind
    project_id: str
    run_id: str | None = None
    params: dict = field(default_factory=dict)


class WorkflowOperation(Protocol):
    def __call__(
        self,
        request: ExecutionRequest,
        storage: BackendStorage,
        progress: ProgressFn,
        bind_client: Callable[[LLMClient], None],
    ) -> str:
        """Run domain work and return its completed project status."""
        ...


@dataclass(frozen=True)
class ExecutionDependencies:
    projects: ProjectRepository
    jobs: JobRepository
    storage_for: Callable[[str], BackendStorage]
    telemetry: Telemetry
    operation: WorkflowOperation
    run_sync: WorkflowDispatcher
    metrics: Metrics = field(default_factory=Metrics)


@dataclass(frozen=True)
class ExportRequest:
    project_id: str
    export_id: int
    run_id: str | None = None
    options: dict = field(default_factory=dict)
