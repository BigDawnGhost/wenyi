"""Separate Arq workflow and export queues share the same domain implementation."""

from __future__ import annotations

import asyncio
import logging

from arq import create_pool
from arq.connections import RedisSettings

from ..config import settings
from ..db import close_pool, init_pool

WORKFLOW_QUEUE = "wenyi:workflows"
EXPORT_QUEUE = "wenyi:exports"


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def enqueue(name: str, **kwargs):
    pool = await create_pool(_redis_settings())
    try:
        return await pool.enqueue_job(
            name, _queue_name=EXPORT_QUEUE if name == "run_export" else WORKFLOW_QUEUE, **kwargs
        )
    finally:
        await pool.aclose()


async def _recovery_loop(ctx: dict) -> None:
    while True:
        try:
            await recover_jobs(ctx)
        except Exception:
            logging.getLogger(__name__).exception("Could not inspect interrupted tasks")
        await asyncio.sleep(30)


async def startup(ctx: dict) -> None:
    init_pool(settings.psycopg_dsn)
    # Maintenance must run even while a long translation occupies every job slot.
    ctx["recovery_task"] = asyncio.create_task(_recovery_loop(ctx))


async def shutdown(ctx: dict) -> None:
    task = ctx.pop("recovery_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    close_pool()


from .recovery import recover_jobs  # noqa: E402
from .tasks import (  # noqa: E402
    run_chapter_translation,
    run_export,
    run_parse,
    run_prepare,
    run_review,
    run_srt,
    run_translation,
)


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
