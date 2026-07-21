"""Postgres 连接池（psycopg3 同步）。

内核与 API 共用同一连接池。内核是同步的，直接用；FastAPI 的同步端点
（``def`` 而非 ``async def``）自动跑在线程池，也不会阻塞事件循环。
"""

from __future__ import annotations

import pathlib
from typing import Optional

from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool] = None
_SCHEMA_SQL = (pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def init_pool(dsn: str) -> ConnectionPool:
    """创建进程级连接池并初始化 schema（幂等）。"""
    global _pool
    if _pool is not None:
        return _pool
    _pool = ConnectionPool(dsn, min_size=1, max_size=16, open=True)
    with _pool.connection() as conn:
        conn.execute(_SCHEMA_SQL)
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized; call init_pool() first.")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
