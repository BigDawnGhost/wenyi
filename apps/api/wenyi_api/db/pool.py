"""Synchronous PostgreSQL connection pool using psycopg 3.

The core and API share a pool within each process. Synchronous core operations use
it directly; FastAPI runs synchronous endpoints in its thread pool to avoid
blocking the event loop.
"""

from __future__ import annotations

import pathlib
from threading import Lock
from typing import Any, Optional, cast

from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool[Any]] = None
_init_lock = Lock()
_SCHEMA_SQL = (pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def init_pool(dsn: str) -> ConnectionPool[Any]:
    """Create the process-wide connection pool and initialize the schema once."""
    global _pool
    with _init_lock:
        if _pool is not None:
            return _pool
        pool = ConnectionPool(
            dsn, min_size=1, max_size=16, open=True, kwargs={"client_encoding": "UTF8"}
        )
        try:
            with pool.connection() as conn:
                # API and both workers may boot together on a fresh database.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('wenyi:schema',0))")
                conn.execute(cast(Any, _SCHEMA_SQL))
        except BaseException:
            pool.close()
            raise
        _pool = pool
        return pool


def get_pool() -> ConnectionPool[Any]:
    if _pool is None:
        raise RuntimeError("DB pool not initialized; call init_pool() first.")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
