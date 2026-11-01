"""Bind scoped HTTP adapters to explicit application-service inputs.

Only the HTTP boundary resolves the request context. Services can be invoked with
independent ports outside an ASGI request and never discover a platform themselves.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from wenyi_core.config import Config
from wenyi_core.ingest.models import Document
from wenyi_core.storage.protocol import Storage

from .. import job_service, project_service, source_view
from ..context import BackendStorage, current_context
from ..workers import enqueue


def require_project(pid: str) -> dict:
    return project_service.require_project(pid, current_context().repository)


def storage_for(pid: str) -> BackendStorage:
    return current_context().storage_for(pid)


def read_storage_for(pid: str) -> Storage:
    return current_context().read_storage_for(pid)


@contextmanager
def project_write(pid: str) -> Iterator[tuple[dict, BackendStorage]]:
    context = current_context()
    with project_service.project_write(pid, context.repository, context.storage_for) as value:
        yield value


def effective_config(
    project: dict, *, document: dict | None = None, defaults: Config | None = None
) -> Config:
    context = current_context()
    return project_service.effective_config(
        project,
        document=document,
        defaults=defaults if defaults is not None else context.settings_store.load().config,
        state_dir=context.project_dir(project["id"]),
    )


def parsed_source(project: dict, storage: Storage) -> Document | None:
    return source_view.parsed_source(project, storage, effective_config)


def chapter_summaries(project: dict) -> list[dict]:
    context = current_context()
    return source_view.chapter_summaries(
        project, context.repository, context.read_storage_for, effective_config
    )


async def start_job(pid: str, kind: str, *, params: dict | None = None) -> dict:
    context = current_context()
    return await job_service.start_job(
        pid,
        kind,
        projects=context.repository,
        jobs=context.repository,
        storage_for=context.storage_for,
        config_for=effective_config,
        enqueue=enqueue,
        params=params,
    )
