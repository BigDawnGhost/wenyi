"""Unit contracts for PostgresStorage.state_lock savepoint nesting.

A fake pool emulates PostgreSQL transaction and aborted-state rules, so these
contracts run without WENYI_TEST_DATABASE_URL: a nested failure must roll back
to its savepoint and leave the outer transaction usable, matching the
SqliteStorage.state_lock reference behavior.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from wenyi_api.storage_pg import PostgresStorage


class FakeCursor:
    def __init__(self, rows=None) -> None:
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """Emulate psycopg3 nested transactions plus PostgreSQL aborted-state rules."""

    def __init__(self) -> None:
        self.log = []
        self.in_transaction = False
        self.aborted = False

    def execute(self, query, params=None):
        if self.aborted and not query.lstrip().upper().startswith("ROLLBACK"):
            raise RuntimeError("current transaction is aborted (25P02)")
        self.log.append(query)
        if query == "FAIL":
            self.aborted = True
            raise RuntimeError("boom")
        return FakeCursor()

    @contextmanager
    def transaction(self):
        if not self.in_transaction:
            self.execute("BEGIN")
            self.in_transaction = True
            try:
                yield self
            except BaseException:
                self.execute("ROLLBACK")
                self.in_transaction = False
                self.aborted = False
                raise
            self.execute("COMMIT")
            self.in_transaction = False
        else:
            self.execute("SAVEPOINT state_nested")
            try:
                yield self
            except BaseException:
                self.execute("ROLLBACK TO SAVEPOINT state_nested")
                self.aborted = False
                self.execute("RELEASE SAVEPOINT state_nested")
                raise
            self.execute("RELEASE SAVEPOINT state_nested")


class FakePool:
    def __init__(self) -> None:
        self.connections = []

    @contextmanager
    def connection(self):
        conn = FakeConnection()
        self.connections.append(conn)
        try:
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
                conn.in_transaction = False


def test_nested_state_lock_failure_rolls_back_to_savepoint_and_outer_continues():
    pool = FakePool()
    storage = PostgresStorage("proj", pool)
    with storage.state_lock():
        conn = pool.connections[-1]
        conn.execute("UPDATE projects SET title='outer'")
        with pytest.raises(RuntimeError, match="boom"):
            with storage.state_lock():
                conn.execute("UPDATE projects SET title='nested'")
                conn.execute("FAIL")
        assert not conn.aborted, "savepoint rollback must clear the aborted state"
        conn.execute("UPDATE projects SET title='after'")
    assert conn.log == [
        "BEGIN",
        "SELECT pg_advisory_xact_lock(%s)",
        "UPDATE projects SET title='outer'",
        "SAVEPOINT state_nested",
        "UPDATE projects SET title='nested'",
        "FAIL",
        "ROLLBACK TO SAVEPOINT state_nested",
        "RELEASE SAVEPOINT state_nested",
        "UPDATE projects SET title='after'",
        "COMMIT",
    ]
    assert not conn.in_transaction


def test_nested_state_lock_failure_propagating_rolls_back_whole_transaction():
    pool = FakePool()
    storage = PostgresStorage("proj", pool)
    with pytest.raises(RuntimeError, match="boom"):
        with storage.state_lock():
            conn = pool.connections[-1]
            conn.execute("UPDATE projects SET title='outer'")
            with storage.state_lock():
                conn.execute("FAIL")
    assert conn.log == [
        "BEGIN",
        "SELECT pg_advisory_xact_lock(%s)",
        "UPDATE projects SET title='outer'",
        "SAVEPOINT state_nested",
        "FAIL",
        "ROLLBACK TO SAVEPOINT state_nested",
        "RELEASE SAVEPOINT state_nested",
        "ROLLBACK",
    ]
    assert not conn.in_transaction
    assert not conn.aborted


def test_nested_state_lock_python_error_rolls_back_nested_writes_only():
    pool = FakePool()
    storage = PostgresStorage("proj", pool)
    with storage.state_lock():
        conn = pool.connections[-1]
        conn.execute("UPDATE projects SET title='outer'")
        with pytest.raises(RuntimeError, match="interrupted"):
            with storage.state_lock():
                conn.execute("UPDATE projects SET title='nested'")
                raise RuntimeError("interrupted")
        conn.execute("UPDATE projects SET title='after'")
    assert "ROLLBACK TO SAVEPOINT state_nested" in conn.log
    assert "UPDATE projects SET title='nested'" in conn.log
    assert "UPDATE projects SET title='after'" in conn.log
    assert conn.log[-1] == "COMMIT"


def test_nested_state_lock_success_releases_savepoint_and_commits():
    pool = FakePool()
    storage = PostgresStorage("proj", pool)
    with storage.state_lock():
        conn = pool.connections[-1]
        with storage.state_lock():
            conn.execute("UPDATE projects SET title='nested'")
        conn.execute("UPDATE projects SET title='after'")
    assert conn.log == [
        "BEGIN",
        "SELECT pg_advisory_xact_lock(%s)",
        "SAVEPOINT state_nested",
        "UPDATE projects SET title='nested'",
        "RELEASE SAVEPOINT state_nested",
        "UPDATE projects SET title='after'",
        "COMMIT",
    ]


def test_deeply_nested_state_lock_releases_each_savepoint():
    pool = FakePool()
    storage = PostgresStorage("proj", pool)
    with storage.state_lock():
        with storage.state_lock():
            with storage.state_lock():
                conn = pool.connections[-1]
                conn.execute("UPDATE projects SET title='deep'")
    assert conn.log.count("SAVEPOINT state_nested") == 2
    assert conn.log.count("RELEASE SAVEPOINT state_nested") == 2
    assert conn.log[-1] == "COMMIT"
