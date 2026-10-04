"""Arq task submission; queue configuration stays in the Web adapter."""

from arq import create_pool
from arq.connections import RedisSettings

WORKFLOW_QUEUE = "wenyi:workflows"
EXPORT_QUEUE = "wenyi:exports"


async def enqueue(redis_url: str, name: str, **kwargs):
    pool = await create_pool(RedisSettings.from_dsn(redis_url))
    try:
        return await pool.enqueue_job(
            name, _queue_name=EXPORT_QUEUE if name == "run_export" else WORKFLOW_QUEUE, **kwargs
        )
    finally:
        await pool.aclose()
