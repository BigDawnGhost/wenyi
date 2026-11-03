"""Reading an old database has to be complete and has to leave the workspace alone."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from wenyi_desktop.migration import VerifyReport, open_read_only
from wenyi_desktop.migration.protocol import LOG_SUFFIX, Migration


def listing(directory: Path) -> list[tuple[str, int]]:
    """Every file in a directory with its size, as a stable fingerprint."""
    return sorted((path.name, path.stat().st_size) for path in directory.iterdir())


def wal_database(directory: Path) -> Path:
    """A database in the journal mode the application sets, closed cleanly.

    A clean close checkpoints the log away, so this is the state an orderly
    engine shutdown leaves behind.
    """
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "local.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE projects(id TEXT PRIMARY KEY)")
    connection.commit()
    connection.execute("INSERT INTO projects VALUES ('kept')")
    connection.commit()
    connection.close()
    return database


@pytest.fixture
def database_with_log(tmp_path: Path):
    """A database whose newest row is still in its log, with the log on disk."""
    directory = tmp_path / "workspace"
    directory.mkdir()
    database = directory / "local.sqlite3"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE projects(id TEXT PRIMARY KEY)")
    writer.commit()
    writer.execute("INSERT INTO projects VALUES ('kept')")
    writer.commit()
    try:
        yield database
    finally:
        writer.close()


def test_rows_that_are_still_in_the_log_are_readable(database_with_log: Path) -> None:
    # The row was committed while the writer was open, so it lives in the log
    # rather than in the database file. A read that ignores the log reports an
    # empty table, which is exactly the silent loss this test guards against.
    assert database_with_log.with_name(database_with_log.name + LOG_SUFFIX).is_file()
    with open_read_only(database_with_log) as connection:
        assert connection.execute("SELECT id FROM projects").fetchall() == [("kept",)]


def test_the_workspace_keeps_exactly_the_files_the_old_version_wrote(
    database_with_log: Path,
) -> None:
    before = listing(database_with_log.parent)
    contents = database_with_log.read_bytes()
    with open_read_only(database_with_log) as connection:
        connection.execute("SELECT count(*) FROM projects").fetchone()
    # Opening a WAL database read-only creates its shared-memory companions
    # beside it; reading a copy is what keeps them out of the workspace.
    assert listing(database_with_log.parent) == before
    assert database_with_log.read_bytes() == contents


def test_the_source_log_survives_being_read(database_with_log: Path) -> None:
    log = database_with_log.with_name(database_with_log.name + LOG_SUFFIX)
    before = log.read_bytes()
    with open_read_only(database_with_log) as connection:
        connection.execute("SELECT count(*) FROM projects").fetchone()
    assert log.read_bytes() == before


def test_the_connection_cannot_write(database_with_log: Path) -> None:
    with open_read_only(database_with_log) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO projects VALUES ('extra')")
    with open_read_only(database_with_log) as connection:
        assert connection.execute("SELECT id FROM projects").fetchall() == [("kept",)]


def test_a_closed_database_without_a_log_is_still_readable(tmp_path: Path) -> None:
    directory = tmp_path / "workspace"
    database = wal_database(directory)
    assert not database.with_name(database.name + LOG_SUFFIX).exists()
    before = listing(directory)
    with open_read_only(database) as connection:
        assert connection.execute("SELECT id FROM projects").fetchall() == [("kept",)]
    assert listing(directory) == before


def test_a_database_in_rollback_journal_mode_is_read_without_companions(tmp_path: Path) -> None:
    directory = tmp_path / "workspace"
    directory.mkdir()
    database = directory / "glossary.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE terms(source TEXT)")
    connection.execute("INSERT INTO terms VALUES ('wenyi')")
    connection.commit()
    connection.close()
    before = listing(directory)
    with open_read_only(database) as reader:
        assert reader.execute("SELECT source FROM terms").fetchall() == [("wenyi",)]
    assert listing(directory) == before


def test_immutable_ignores_the_log_and_loses_committed_rows(database_with_log: Path) -> None:
    # The reason the flag is banned in this package rather than avoided by
    # convention: it reports success and returns nothing. If a future SQLite
    # changes this, revisit the module docstring before relaxing the rule.
    uri = database_with_log.as_uri() + "?mode=ro&immutable=1"
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(uri, uri=True).execute("SELECT id FROM projects").fetchall()


def test_a_missing_database_is_reported_rather_than_created(tmp_path: Path) -> None:
    missing = tmp_path / "workspace" / "local.sqlite3"
    with pytest.raises(FileNotFoundError):
        with open_read_only(missing):
            pass
    assert not missing.exists()


def test_the_report_is_clean_only_when_nothing_is_missing() -> None:
    assert VerifyReport(files=2, total_bytes=8).ok
    assert not VerifyReport(problems=("local.sqlite3",)).ok
    assert VerifyReport().ok


def test_a_complete_migration_satisfies_the_protocol() -> None:
    class Complete:
        def preflight(self) -> None:
            return None

        def snapshot(self) -> Path:
            return Path("snapshot")

        def apply(self) -> None:
            return None

        def verify(self) -> VerifyReport:
            return VerifyReport()

        def rollback(self) -> None:
            return None

    assert isinstance(Complete(), Migration)


def test_an_incomplete_migration_does_not_satisfy_the_protocol() -> None:
    class Partial:
        def preflight(self) -> None:
            return None

    assert not isinstance(Partial(), Migration)
