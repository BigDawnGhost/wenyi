"""Explicit application ports with request- and task-scoped access.

The ContextVar is a bridge for synchronous service functions, not a platform
selector. Every ASGI invocation and worker entry binds its owning application.
AnyIO and asyncio.to_thread copy this scope; raw threads must copy it explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    ContextManager,
    Literal,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from fastapi import WebSocket
from wenyi_core.config import Config
from wenyi_core.llm.base import LLMClient
from wenyi_core.models.document import Chapter
from wenyi_core.pdf_bridge.connection import BridgeResolver
from wenyi_core.storage.chapter_reading import ChapterDigestRow
from wenyi_core.storage.protocol import Storage

from .live_statistics import StatisticsCache
from .metrics import Metrics
from .ports.jobs import JobCatalog
from .ports.projects import ProjectCatalog

if TYPE_CHECKING:
    from .global_settings import GlobalSettings


@runtime_checkable
class BackendStorage(Storage, Protocol):
    """Project state with the additional reads and locks required by the backend."""

    def lock(self, *, blocking: bool = True) -> ContextManager[None]: ...
    def export_lock(self, export_id: int, *, blocking: bool = True) -> ContextManager[None]: ...
    def save_chapter(
        self, chapter: Chapter, *, revision_kind: Literal["manual"] | None = None
    ) -> None: ...
    def set_chapter_review_status(self, ci: int, status: str) -> None: ...
    def load_segment_history(self, ci: int, si: int) -> list[dict]: ...
    def read_chapter_digests(self) -> list[ChapterDigestRow]:
        """Read digests in inventory order, raising ChapterNotFound for missing chapters."""
        ...


class Repository(ProjectCatalog, JobCatalog, Protocol):
    """Composition aggregate, not an implicit service locator; consumers use narrower ports."""

    def create_export(self, pid: str, fmt: str, options: dict) -> int: ...
    def set_export_status(
        self,
        export_id: int,
        status: str,
        *,
        path: str | None = None,
        size: int | None = None,
        error: str | None = None,
    ) -> None: ...


class SettingsStore(Protocol):
    def defaults(self) -> Config: ...
    def load(self, *, connection: Any = None) -> GlobalSettings: ...
    def guard(self, *, exclusive: bool = False) -> ContextManager[Any]: ...
    def save(
        self,
        value: str,
        default_template: str,
        revision: int,
        *,
        model_renames: dict[str, str] | None = None,
        provider_renames: dict[str, str] | None = None,
    ) -> GlobalSettings: ...
    def project_configs(self, connection: Any) -> list[tuple[str, dict]]: ...


@dataclass(frozen=True)
class ExportAdmission:
    export_id: int
    job_id: int
    run_id: str
    created: bool


class ExportStore(Protocol):
    def list_exports(self, pid: str) -> list[dict]: ...
    def admit_export(
        self, pid: str, fmt: str, options: dict, run_id: str, snapshot: dict, *, source: dict
    ) -> ExportAdmission: ...
    def fail_export_admission(self, job_id: int, error: str) -> Any: ...
    def open_export(self, pid: str, export_id: int) -> tuple[BinaryIO, Path]: ...
    def publish_export(
        self, pid: str, export_id: int, output: str, *, job_id: int | None = None
    ) -> None:
        """Publish the export and complete its job in one catalog transaction."""
        ...


class SourcePaths(Protocol):
    """Host-owned source identities, independent of the Web data-volume layout."""

    def source_reference(self, source: Path) -> str: ...
    def source_path(self, pid: str, reference: str) -> Path: ...


class Telemetry(Protocol):
    def connect(self) -> StatisticsCache: ...
    def release(self, cache: StatisticsCache) -> None: ...
    def progress_snapshot(self, pid: str) -> dict | None: ...
    def statistics_snapshot(self, pid: str, job: dict) -> dict | None: ...
    async def relay(self, websocket: WebSocket, pid: str) -> None: ...


_WorkflowResult = TypeVar("_WorkflowResult")


class WorkflowDispatcher(Protocol):
    """Run a synchronous workflow with its context, retaining ownership until it exits."""

    async def __call__(self, operation: Callable[[], _WorkflowResult], /) -> _WorkflowResult: ...


@dataclass(frozen=True)
class BackendContext:
    """One application's adapters; never inferred from environment or imports."""

    repository: Repository
    settings_store: SettingsStore
    exports: ExportStore
    telemetry: Telemetry
    storage_for: Callable[[str], BackendStorage]
    read_storage_for: Callable[[str], Storage]
    build_client: Callable[[Config], LLMClient]
    enqueue: Callable[..., Awaitable[Any]]
    data_dir: str
    project_dir: Callable[[str], str]
    health: Callable[[], None]
    update_term: Callable[..., None]
    remove_project: Callable[[str], None]
    api_token: str | None = None
    #: Where a project that does not exist yet will be created, or `ValueError`
    #: carrying the sentence a person reads.
    #:
    #: The shared application has no filesystem of its own: which folders exist and
    #: which are safe to write to are facts only the host knows. `location` is the
    #: folder a person chose, or None for this installation's default. `name` is the
    #: project's, because a library directory carries a readable half.
    #:
    #: A host with no library concept leaves this unset. A request that names a
    #: location then fails instead of quietly creating the project somewhere else:
    #: a client must never be told the project went where it asked when it did not.
    new_project_dir: Callable[[str, str, str | None], Path] | None = None
    # None retains DATA_DIR-relative Web originals. Desktop keeps movable project-local names.
    source_paths: SourcePaths | None = None
    mineru_token_resolver: Callable[[], str | None] | None = None
    # A draft without a saved ID may use its environment, never another connection's key.
    connection_credential: Callable[[str | None, Any], str | None] | None = None
    babeldoc_connection: BridgeResolver | None = None
    # Web retains its executor; Desktop owns a dedicated thread per workflow.
    run_workflow_sync: WorkflowDispatcher = asyncio.to_thread
    metrics: Metrics = field(default_factory=Metrics)
    # Optional host hooks for automatic resume after resumable provider outages.
    schedule_auto_resume: Callable[[Any, int, int, str], None] | None = None
    cancel_auto_resume: Callable[[str], None] | None = None
    # Auth policy: with api_token set, bearer checks stay on unless the host
    # explicitly opted out. Default True keeps deployments fail-closed.
    require_token: bool = True


_context: ContextVar[BackendContext] = ContextVar("wenyi_backend_context")


def current_context() -> BackendContext:
    try:
        return _context.get()
    except LookupError as error:
        raise RuntimeError("Bind a BackendContext before calling backend services") from error


@contextmanager
def use_context(context: BackendContext) -> Iterator[BackendContext]:
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


class ContextMiddleware:
    def __init__(self, app: Any, context: BackendContext):
        self.app = app
        self.context = context

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        with use_context(self.context):
            await self.app(scope, receive, send)
