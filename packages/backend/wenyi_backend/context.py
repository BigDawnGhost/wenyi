"""Explicit application ports with request- and task-scoped access.

The ContextVar is a bridge for synchronous service functions, not a platform
selector. Every ASGI invocation and worker entry binds its owning application.
AnyIO and asyncio.to_thread copy this scope; raw threads must copy it explicitly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, ContextManager, Protocol

from fastapi import WebSocket
from wenyi_core.config import Config
from wenyi_core.llm.base import LLMClient
from wenyi_core.storage.protocol import Storage

from .live_statistics import StatisticsCache

if TYPE_CHECKING:
    from .global_settings import GlobalSettings


class Repository(Protocol):
    """Catalog operations; connection tokens are opaque to shared services."""

    def create_project(
        self,
        name: str,
        source_lang: str,
        target_lang: str,
        strategy: dict,
        *,
        project_id: str | None = None,
        source: dict | None = None,
        config: dict | None = None,
        connection: Any = None,
    ) -> str: ...
    def get_project(self, pid: str) -> dict | None: ...
    def list_projects(self) -> list[dict]: ...
    def delete_project(self, pid: str) -> None: ...
    def set_project_status(self, pid: str, status: str, *, error: str | None = None) -> None: ...
    def set_project_strategy(self, pid: str, strategy: dict) -> None: ...
    def set_project_config(self, pid: str, config: dict, *, connection: Any = None) -> None: ...
    def set_project_languages(
        self, pid: str, source_lang: str, target_lang: str, *, connection: Any = None
    ) -> None: ...
    def set_project_source(
        self,
        pid: str,
        source_path: str,
        book_title: str | None,
        *,
        source_sha256: str | None = None,
        fmt: str | None = None,
        source_meta: dict | None = None,
    ) -> None: ...
    def chapter_summaries(self, pid: str) -> list[dict]: ...
    def total_word_count(self, pid: str) -> int: ...
    def create_job(
        self,
        pid: str,
        kind: str,
        arq_job_id: str,
        *,
        params: dict | None = None,
        config_snapshot: dict | None = None,
        run_id: str | None = None,
        status: str = "queued",
    ) -> int: ...
    def admit_job(
        self, pid: str, kind: str, job_id: str, *, project_status: str, **kwargs: Any
    ) -> int: ...
    def fail_admission(self, job_id: int, previous_status: str, error: str, /) -> Any: ...
    def get_job(self, job_id: int) -> dict | None: ...
    def get_job_by_arq_id(self, arq_job_id: str) -> dict | None: ...
    def set_job_status(
        self, job_id: int, status: str, error: str | None = None, *, result: dict | None = None
    ) -> Any: ...
    def list_jobs(self, pid: str) -> list[dict]: ...
    def latest_resumable_job(self, pid: str) -> dict | None: ...
    def job_review_id(self, job_id: int) -> str | None: ...
    def is_paused(self, pid: str) -> bool: ...
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


@dataclass(frozen=True)
class BackendContext:
    """One application's adapters; never inferred from environment or imports."""

    repository: Repository
    settings_store: SettingsStore
    exports: ExportStore
    telemetry: Telemetry
    storage_for: Callable[[str], Storage]
    build_client: Callable[[Config], LLMClient]
    enqueue: Callable[..., Awaitable[Any]]
    data_dir: str
    project_dir: Callable[[str], str]
    health: Callable[[], None]
    update_term: Callable[..., None]
    api_token: str | None = None


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
