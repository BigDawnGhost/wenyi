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


def init_pool(dsn: str) -> ConnectionPool[Any]:
    """Create an application-owned pool and initialize the schema transactionally."""
    pool = ConnectionPool(
        dsn, min_size=1, max_size=16, open=True, kwargs={"client_encoding": "UTF8"}
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
