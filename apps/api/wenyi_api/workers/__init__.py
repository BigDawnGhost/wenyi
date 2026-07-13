"""Arq 任务队列：长时翻译 / 导出在 worker 进程执行。

API 进程通过 :func:`enqueue` 投递任务到 Redis；worker 进程
（``arq wenyi_api.workers.WorkerSettings``）消费。
"""

from __future__ import annotations

from arq import create_pool
from arq.connections import RedisSettings

from ..config import settings
from ..db import close_pool, init_pool


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def enqueue(name: str, **kwargs):
    """从 API 进程投递一个 Arq 任务。返回 arq Job（或 None）。"""
    pool = await create_pool(_redis_settings())
    try:
        return await pool.enqueue_job(name, **kwargs)
    finally:
        await pool.close()


async def startup(ctx: dict) -> None:
    """Worker 启动时初始化 Postgres 连接池。"""
    init_pool(settings.psycopg_dsn)


async def shutdown(ctx: dict) -> None:
    close_pool()


# 真正的任务函数（worker 进程执行）
from .tasks import run_export, run_translation  # noqa: E402


class WorkerSettings:
    """``arq wenyi_api.workers.WorkerSettings`` 入口。"""
    functions = [run_translation, run_export]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _redis_settings()
    max_jobs = 1  # 翻译是重任务；单 worker 串行避免资源争抢
