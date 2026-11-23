"""Export publication ordering: history rows commit first, files are removed after."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from psycopg_pool import ConnectionPool
from wenyi_api import export_retention


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection

    def fetchone(self):
        return self._connection.fetchone()

    def fetchall(self):
        return self._connection.fetchall()


class FakeConnection:
    """Minimal psycopg connection stand-in that records statement order."""

    def __init__(self, update_result=None, expired_result=None, fail_on=None):
        self.statements = []
        self.commit_marks = []
        self.rollback_count = 0
        self.update_result = update_result
        self.expired_result = expired_result or []
        self.fail_on = fail_on
        self.watch: Path | None = None
        self._pending = ""

    def execute(self, statement, params=None):
        if self.fail_on and self.fail_on in statement:
            raise RuntimeError("database unavailable")
        self.statements.append(statement)
        self._pending = statement
        return FakeCursor(self)

    def fetchone(self):
        if "hashtextextended" in self._pending:
            return (True,)
        if "UPDATE exports" in self._pending:
            return self.update_result
        return None

    def fetchall(self):
        if "SELECT id,path,format" in self._pending:
            return self.expired_result
        return []

    def commit(self):
        exists = self.watch.exists() if self.watch is not None else None
        self.commit_marks.append((len(self.statements), exists))

    def rollback(self):
        self.rollback_count += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class FakePool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        return self._connection


def _prepare(data_dir, expired=None, fail_on=None):
    old_file = Path(data_dir) / "p1" / "exports" / "e-old" / "translation.txt"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("old export", encoding="utf-8")
    output = Path(data_dir) / "p1" / "exports" / "e-new" / "translation.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("new export", encoding="utf-8")
    conn = FakeConnection(
        update_result=(7,),
        expired_result=expired or [],
        fail_on=fail_on,
    )
    conn.watch = old_file
    return old_file, output, conn


def test_publish_commits_history_before_removing_expired_files(tmp_path):
    data_dir = tmp_path / "data"
    old_file, output, conn = _prepare(
        data_dir, expired=[(1, "p1/exports/e-old/translation.txt", "txt")]
    )
    export_retention.publish_export(
        cast(ConnectionPool, FakePool(conn)), "p1", 7, str(output), data_dir=str(data_dir)
    )
    delete_at = next(
        index for index, sql in enumerate(conn.statements) if "DELETE FROM exports" in sql
    )
    commit_at, old_file_at_commit = conn.commit_marks[0]
    # The history row disappears inside the transaction, while the file only goes
    # away in the compensating cleanup that runs after the commit.
    assert delete_at < commit_at and old_file_at_commit is True
    session_lock = next(sql for sql in conn.statements if "pg_advisory_lock(" in sql)
    assert conn.statements.index(session_lock) >= commit_at
    assert any("pg_advisory_unlock" in sql for sql in conn.statements)
    assert not old_file.exists() and output.exists()


def test_publish_database_failure_keeps_files_and_rolls_back(tmp_path):
    data_dir = tmp_path / "data"
    old_file, output, conn = _prepare(
        data_dir,
        expired=[(1, "p1/exports/e-old/translation.txt", "txt")],
        fail_on="DELETE FROM exports",
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        export_retention.publish_export(
            cast(ConnectionPool, FakePool(conn)), "p1", 7, str(output), data_dir=str(data_dir)
        )
    # The transaction rolled back, so no compensating cleanup may touch files.
    assert conn.rollback_count == 1
    assert all("pg_advisory_lock(" not in sql for sql in conn.statements)
    assert old_file.exists() and output.exists()


def test_publish_without_expired_history_skips_file_cleanup(tmp_path):
    data_dir = tmp_path / "data"
    _old_file, output, conn = _prepare(data_dir)
    export_retention.publish_export(
        cast(ConnectionPool, FakePool(conn)), "p1", 7, str(output), data_dir=str(data_dir)
    )
    assert all("pg_advisory_lock(" not in sql for sql in conn.statements)
    assert not any("DELETE FROM exports" in sql for sql in conn.statements)
    assert output.exists()
