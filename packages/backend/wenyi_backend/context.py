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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ContextManager, Protocol, runtime_checkable

from fastapi import WebSocket
from wenyi_core.config import Config
from wenyi_core.llm.base import LLMClient
from wenyi_core.pdf_bridge.connection import BridgeResolver
from wenyi_core.storage.protocol import Storage

from .live_statistics import StatisticsCache
from .ports.jobs import JobCatalog
from .ports.projects import ProjectCatalog

if TYPE_CHECKING:
    from .global_settings import GlobalSettings


@runtime_checkable
class BackendStorage(Storage, Protocol):
    """Project state with the session and per-export locks required by backend workers."""

    def lock(self, *, blocking: bool = True) -> ContextManager[None]: ...
    def export_lock(self, export_id: int, *, blocking: bool = True) -> ContextManager[None]: ...


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


class ExportStore(Protocol):
    def list_exports(self, pid: str) -> list[dict]: ...
    def admit_export(
        self, pid: str, fmt: str, options: dict, run_id: str, snapshot: dict
    ) -> tuple[int, int]: ...
    def fail_export_admission(self, job_id: int, error: str) -> Any: ...
    def open_export(self, pid: str, export_id: int) -> tuple[BinaryIO, Path]: ...
    def publish_export(self, pid: str, export_id: int, output: str) -> None: ...


class Telemetry(Protocol):
    def connect(self) -> StatisticsCache: ...
    def release(self, cache: StatisticsCache) -> None: ...
    def progress_snapshot(self, pid: str) -> dict | None: ...
    def statistics_snapshot(self, pid: str, job: dict) -> dict | None: ...
    async def relay(self, websocket: WebSocket, pid: str) -> None: ...


class WorkflowDispatcher(Protocol):
    """Run a synchronous workflow with its context, retaining ownership until it exits."""

    async def __call__(self, operation: Callable[[], None], /) -> None: ...


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
    mineru_token_resolver: Callable[[], str | None] | None = None
    # Desktop resolves a saved connection's stored key; None keeps environment lookup (Web).
    connection_credential: Callable[[str, Any], str | None] | None = None
    babeldoc_connection: BridgeResolver | None = None
    # Web retains its executor; Desktop owns a dedicated thread per workflow.
    run_workflow_sync: WorkflowDispatcher = asyncio.to_thread


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
