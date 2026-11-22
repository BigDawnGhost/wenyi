"""Session-lock contracts for PostgresStorage that need no database service.

A fake psycopg.connect stands in for PostgreSQL so these tests can assert that
lock spans open dedicated connections and never check connections out of the
shared pool, while keeping the documented blocking, re-entrancy and release
semantics.
"""

from __future__ import annotations

import pytest
import wenyi_api.storage_pg as storage_pg
from wenyi_api.storage_pg import PostgresStorage, ProjectBusyError


class FakeCursor:
    """Single-row cursor, enough for advisory-lock boolean results."""

    def __init__(self, row: tuple) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class FakeLockConnection:
    """Emulate one psycopg connection hosting a session advisory lock."""

    def __init__(self, try_result: bool = True) -> None:
        self.log: list[str] = []
        self.conninfo = ""
        self.kwargs: dict = {}
        self.closed = False
        self._try_result = try_result

    def execute(self, query: str, params=None) -> FakeCursor:
        self.log.append(query)
        if query == "SELECT pg_try_advisory_lock(%s)":
            return FakeCursor((self._try_result,))
        return FakeCursor((True,))

    def commit(self) -> None:
        self.log.append("COMMIT")

    def rollback(self) -> None:
        self.log.append("ROLLBACK")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.closed = True
        return False


class LockOnlyPool:
    """Exposes only the connection facts a lock needs: no checkout API exists."""

    conninfo = "postgresql://wenyi:wenyi@localhost:5432/wenyi"
    kwargs = {"client_encoding": "UTF8"}


class LockHarness:
    """Record every connection lock spans open, plus scripted try-lock results."""

    def __init__(self) -> None:
        self.connections: list[FakeLockConnection] = []
        self.try_results: list[bool] = []


@pytest.fixture
def lock_harness(monkeypatch) -> LockHarness:
    harness = LockHarness()

    def fake_connect(conninfo, **kwargs):
        conn = FakeLockConnection(
            try_result=harness.try_results.pop(0) if harness.try_results else True
        )
        conn.conninfo = conninfo
        conn.kwargs = kwargs
        conn.log.append(f"connect:{conninfo}")
        harness.connections.append(conn)
        return conn

    monkeypatch.setattr(storage_pg.psycopg, "connect", fake_connect)
    return harness


def test_lock_span_opens_dedicated_connection_and_releases(lock_harness):
    storage = PostgresStorage("project", LockOnlyPool())
    with storage.lock():
        assert len(lock_harness.connections) == 1
        conn = lock_harness.connections[0]
        assert conn.conninfo == LockOnlyPool.conninfo
        assert conn.kwargs == {"client_encoding": "UTF8"}
        assert "SELECT pg_advisory_lock(%s)" in conn.log
        assert "COMMIT" in conn.log  # no open transaction while the span runs
        assert "SELECT pg_advisory_unlock(%s)" not in conn.log
    assert "SELECT pg_advisory_unlock(%s)" in conn.log
    assert conn.closed


def test_lock_reentry_reuses_span_without_new_connections(lock_harness):
    storage = PostgresStorage("project", LockOnlyPool())
    with storage.lock():
        with storage.lock():
            assert len(lock_harness.connections) == 1
        assert len(lock_harness.connections) == 1
    assert len(lock_harness.connections) == 1
    # Depth bookkeeping must not leak: a later lock acquires cleanly again.
    with storage.lock():
        assert len(lock_harness.connections) == 2


def test_nonblocking_conflict_raises_busy_and_never_unlocks(lock_harness):
    lock_harness.try_results.append(False)
    storage = PostgresStorage("project", LockOnlyPool())
    with pytest.raises(ProjectBusyError):
        with storage.lock(blocking=False):
            pass
    conn = lock_harness.connections[0]
    assert "SELECT pg_try_advisory_lock(%s)" in conn.log
    assert "SELECT pg_advisory_unlock(%s)" not in conn.log
    assert conn.closed


def test_exception_inside_lock_span_releases_and_closes(lock_harness):
    storage = PostgresStorage("project", LockOnlyPool())
    with pytest.raises(RuntimeError, match="boom"):
        with storage.lock():
            raise RuntimeError("boom")
    conn = lock_harness.connections[0]
    assert "SELECT pg_advisory_unlock(%s)" in conn.log
    assert conn.closed
    with storage.lock():
        assert len(lock_harness.connections) == 2


def test_distinct_scopes_hold_distinct_connections(lock_harness):
    storage = PostgresStorage("project", LockOnlyPool())
    with storage.lock(), storage.export_lock(7), storage.assemble_lock():
        assert len(lock_harness.connections) == 3
        assert all(
            conn.log.count("SELECT pg_advisory_lock(%s)") == 1 for conn in lock_harness.connections
        )
    assert all(conn.closed for conn in lock_harness.connections)
