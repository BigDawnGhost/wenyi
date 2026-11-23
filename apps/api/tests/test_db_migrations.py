"""Versioned schema migrations: fresh, legacy, current and too-new databases."""

import pytest
from wenyi_api.db.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    SchemaVersionError,
    run_migrations,
    validate_migration_order,
)

BASELINE = MIGRATIONS[0].sql


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """Records statements and simulates schema_migrations bookkeeping."""

    def __init__(self, versions=()):
        self.statements = []
        self.versions = set(versions)

    def execute(self, sql, params=None):
        self.statements.append(sql if params is None else f"{sql} {params}")
        if "CREATE TABLE IF NOT EXISTS schema_migrations" in sql:
            return FakeResult([])
        if sql.startswith("SELECT version FROM schema_migrations"):
            return FakeResult([(version,) for version in sorted(self.versions)])
        if sql.startswith("INSERT INTO schema_migrations"):
            assert params is not None
            self.versions.add(params[0])
        return FakeResult([])


def test_shipped_migrations_start_at_the_baseline_schema():
    validate_migration_order()

    assert LATEST_VERSION == 1
    assert "CREATE TABLE IF NOT EXISTS projects" in BASELINE


def test_fresh_database_applies_the_baseline_and_records_the_version():
    conn = FakeConnection()

    assert run_migrations(conn) == [1]
    assert any("CREATE TABLE IF NOT EXISTS projects" in sql for sql in conn.statements)
    assert conn.versions == {1}


def test_legacy_database_is_upgraded_in_place_without_destroying_state():
    conn = FakeConnection()
    conn.statements.append("-- legacy database already serves project rows")

    assert run_migrations(conn) == [1]
    assert conn.versions == {1}
    markers = ("DROP TABLE", "TRUNCATE", "DELETE FROM")
    destructive = [sql for sql in conn.statements if any(mark in sql for mark in markers)]
    assert destructive == []


def test_current_database_skips_already_applied_migrations():
    conn = FakeConnection(versions={1})

    assert run_migrations(conn) == [1]
    assert not any("CREATE TABLE IF NOT EXISTS projects" in sql for sql in conn.statements)


def test_pending_migrations_apply_in_version_order():
    migrations = (
        Migration(version=1, sql="CREATE TABLE one()"),
        Migration(version=2, sql="CREATE TABLE two()"),
        Migration(version=3, sql="CREATE TABLE three()"),
    )
    conn = FakeConnection(versions={1})

    assert run_migrations(conn, migrations) == [1, 2, 3]
    applied = [sql for sql in conn.statements if sql.startswith("CREATE TABLE ")]
    assert applied == ["CREATE TABLE two()", "CREATE TABLE three()"]


def test_refuses_a_database_newer_than_the_application():
    conn = FakeConnection(versions={LATEST_VERSION + 1})

    with pytest.raises(SchemaVersionError):
        run_migrations(conn)
    assert not any("CREATE TABLE IF NOT EXISTS projects" in sql for sql in conn.statements)


def test_migration_versions_must_be_positive_and_strictly_increasing():
    with pytest.raises(SchemaVersionError):
        validate_migration_order((Migration(version=1, sql="a"), Migration(version=1, sql="b")))
    with pytest.raises(SchemaVersionError):
        validate_migration_order((Migration(version=2, sql="a"), Migration(version=1, sql="b")))


@pytest.mark.integration
def test_legacy_pg_database_upgrades_in_place(pg_pool):
    """A database built by the old direct schema.sql startup upgrades in place."""
    with pg_pool.connection() as conn:
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        conn.execute(
            "INSERT INTO projects(id,name,source_lang,target_lang) "
            "VALUES('legacy-project','legacy','en','zh')"
        )
    with pg_pool.connection() as conn:
        applied = run_migrations(conn)
    assert applied == [1]
    with pg_pool.connection() as conn:
        versions = conn.execute("SELECT version FROM schema_migrations").fetchall()
        surviving = conn.execute("SELECT name FROM projects WHERE id='legacy-project'").fetchall()
    assert [row[0] for row in versions] == [1]
    assert [row[0] for row in surviving] == ["legacy"]
