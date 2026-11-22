"""Synchronous PostgreSQL connection pool using psycopg 3.

Each application or worker owns its pool. Synchronous core operations use it
directly; FastAPI runs synchronous endpoints in its thread pool to avoid blocking
the event loop. Scope lookup never creates or selects a process-global pool.
"""

from __future__ import annotations

import pathlib
from typing import Any, cast

from psycopg_pool import ConnectionPool
from wenyi_backend.context import current_context

_SCHEMA_SQL = (pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Default shared-pool depth for short request and state connections.
DEFAULT_POOL_MAX_SIZE = 16


def init_pool(dsn: str, *, max_size: int = DEFAULT_POOL_MAX_SIZE) -> ConnectionPool[Any]:
    """Create an application-owned pool and initialize the schema transactionally.

    The pool serves only short request and state connections: workflow, export
    and assembly locks use dedicated connections (see storage_pg). Keep the
    depth aligned with worker max_jobs plus the request thread pool.
    """
    pool = ConnectionPool(
        dsn, min_size=1, max_size=max_size, open=True, kwargs={"client_encoding": "UTF8"}
    )
    try:
        with pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('wenyi:schema',0))")
            conn.execute(cast(Any, _SCHEMA_SQL))
    except BaseException:
        pool.close()
        raise
    return pool


def get_pool() -> ConnectionPool[Any]:
    from ..adapters import postgres_repository

    return postgres_repository(current_context()).pool


def close_pool() -> None:
    from ..adapters import postgres_repository

    postgres_repository(current_context()).close()
