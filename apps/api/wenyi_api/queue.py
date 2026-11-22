"""Arq task submission; queue configuration stays in the Web adapter."""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from wenyi_backend.workers.signing import SIGNATURE_FIELD, sign_task

WORKFLOW_QUEUE = "wenyi:workflows"
EXPORT_QUEUE = "wenyi:exports"

# One pool per distinct Redis URL, kept for the process lifetime. Opening and closing a
# full connection pool around every submission cost a TCP + handshake round trip each
# time (audit C-6); workers and the API run in separate processes, so each keeps its own.
_pools: dict[str, ArqRedis] = {}


async def _pool_for(redis_url: str) -> ArqRedis:
    pool = _pools.get(redis_url)
    if pool is None:
        pool = await create_pool(RedisSettings.from_dsn(redis_url))
        _pools[redis_url] = pool
    return pool


async def enqueue(redis_url: str, name: str, secret: str | None, **kwargs):
    pool = await _pool_for(redis_url)
    signed = {**kwargs, SIGNATURE_FIELD: sign_task(name, kwargs, secret)}
    return await pool.enqueue_job(
        name, _queue_name=EXPORT_QUEUE if name == "run_export" else WORKFLOW_QUEUE, **signed
    )
