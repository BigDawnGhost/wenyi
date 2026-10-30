"""Separate Arq workflow and export queues share the same domain implementation."""

from __future__ import annotations

import asyncio
import logging

from arq.connections import RedisSettings
from wenyi_backend.context import use_context
from wenyi_backend.workers.tasks import (
    run_chapter_translation,
    run_export,
    run_parse,
    run_prepare,
    run_review,
    run_srt,
    run_translation,
)

from ..adapters import create_context, postgres_repository
from ..config import settings
from ..queue import EXPORT_QUEUE, WORKFLOW_QUEUE


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def _recovery_loop(ctx: dict) -> None:
    while True:
        try:
            with use_context(ctx["backend"]):
                await recover_jobs(ctx)
        except Exception:
            logging.getLogger(__name__).exception("Could not inspect interrupted tasks")
        await asyncio.sleep(30)


async def startup(ctx: dict) -> None:
    context = create_context(settings)
    postgres_repository(context).start()
    ctx["backend"] = context
    # Maintenance must run even while a long translation occupies every job slot.
    ctx["recovery_task"] = asyncio.create_task(_recovery_loop(ctx))


async def shutdown(ctx: dict) -> None:
    task = ctx.pop("recovery_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    postgres_repository(ctx["backend"]).close()


from .recovery import recover_jobs  # noqa: E402


class WorkerSettings:
    functions = [
        run_parse,
        run_prepare,
        run_translation,
        run_chapter_translation,
        run_review,
        run_srt,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    queue_name = WORKFLOW_QUEUE
    max_jobs = 1
    job_timeout = 86400
    max_tries = 1


class ExportWorkerSettings:
    functions = [run_export]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    queue_name = EXPORT_QUEUE
    max_jobs = 2
    job_timeout = 3600
    max_tries = 1
