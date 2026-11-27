"""Regression tests for which startup failures may point at upgrade recovery.

An initialization error is not evidence that an upgrade failed. Only a refusal
of the stored data format may route the user to the explicit offline recovery
command, which replaces the whole workspace with a pre-update snapshot; every
other failure has to surface as the error it actually is.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from wenyi_core.errors import SchemaTooNew
from wenyi_desktop import catalog, main, paths, updates
from wenyi_desktop.diagnostics import StartupFailure, StartupFailureCode

ENGINE_TARGET = "1.9.0"
CATALOG_REFUSAL = (
    "This is not a current-format Desktop catalog. "
    "Choose a new Desktop data directory; legacy data is not migrated."
)


def _arm_snapshot(root):
    """Leave an armed pre-update snapshot, so recovery would be genuinely available."""
    database = paths.catalog(root)
    snapshot = root / updates.BACKUPS_DIR / "1.9.0-first"
    snapshot.mkdir(parents=True)
    (snapshot / "catalog.sqlite3").write_bytes(database.read_bytes())
    manifest = snapshot / updates.MANIFEST_NAME
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source_path": "",
                "created_at": "2026-11-20T00:00:00Z",
                "version": "1.9.0",
                "files": [
                    {
                        "path": "catalog.sqlite3",
                        "size": (snapshot / "catalog.sqlite3").stat().st_size,
                        "sha256": hashlib.sha256(
                            (snapshot / "catalog.sqlite3").read_bytes()
                        ).hexdigest(),
                    }
                ],
                "sources": [{"name": "catalog.sqlite3", "path": str(database)}],
            }
        ),
        encoding="utf-8",
    )
    (root / updates.BACKUPS_DIR / updates.ATTEMPT_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot": snapshot.name,
                "target_version": ENGINE_TARGET,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "phase": "prepared",
            }
        ),
        encoding="utf-8",
    )
    return snapshot


@pytest.fixture
def armed_workspace(tmp_path, monkeypatch):
    """A healthy workspace whose interrupted upgrade would be recoverable."""
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", ENGINE_TARGET)
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    catalog.initialize(paths.catalog(root))
    snapshot = _arm_snapshot(root)
    assert updates.upgrade_recovery_available(root, ENGINE_TARGET)
    return root, snapshot, paths.catalog(root)


def _assert_plain_startup_failure(caught, snapshot):
    """The failure reaches the caller as itself, not as an upgrade instruction."""
    assert not isinstance(caught.value, StartupFailure)
    # The armed snapshot is untouched, so recovery is still exactly as reachable
    # as it was before the failed start.
    assert snapshot.is_dir() and (snapshot / updates.MANIFEST_NAME).is_file()


def test_corrupt_catalog_is_not_reported_as_an_upgrade_failure(armed_workspace, capsys):
    root, snapshot, database = armed_workspace
    # A file that is not a database at all: no format the engine could refuse.
    database.write_bytes(b"this is not a SQLite database")

    with pytest.raises(sqlite3.DatabaseError) as caught:
        main.create_context(root)

    _assert_plain_startup_failure(caught, snapshot)
    assert "file is not a database" in str(caught.value)
    assert "WENYI_FAILED" not in capsys.readouterr().out


def test_engine_lock_conflict_is_not_reported_as_an_upgrade_failure(armed_workspace, capsys):
    root, snapshot, database = armed_workspace
    holder = sqlite3.connect(database, isolation_level=None, timeout=0)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            main.create_context(root)
    finally:
        with closing(holder) as connection:
            connection.execute("ROLLBACK")

    _assert_plain_startup_failure(caught, snapshot)
    assert "locked" in str(caught.value)
    assert "WENYI_FAILED" not in capsys.readouterr().out


def test_unreadable_config_path_is_not_reported_as_an_upgrade_failure(
    armed_workspace, tmp_path, capsys
):
    root, snapshot, _ = armed_workspace
    missing = tmp_path / "does-not-exist.yaml"

    with pytest.raises(OSError) as caught:
        main.create_context(root, config_path=str(missing))

    _assert_plain_startup_failure(caught, snapshot)
    assert not isinstance(caught.value, SchemaTooNew)
    assert "does-not-exist.yaml" in str(caught.value)
    assert "WENYI_FAILED" not in capsys.readouterr().out


def test_unparseable_config_file_is_not_reported_as_an_upgrade_failure(
    armed_workspace, tmp_path, capsys
):
    root, snapshot, _ = armed_workspace
    malformed = tmp_path / "config.yaml"
    malformed.write_text("a: [1,\n", encoding="utf-8")

    with pytest.raises(Exception) as caught:
        main.create_context(root, config_path=str(malformed))

    # A parse error is a YAML error, not a format refusal; it must pass through.
    _assert_plain_startup_failure(caught, snapshot)
    assert not isinstance(caught.value, SchemaTooNew)
    assert type(caught.value).__module__.startswith("yaml")
    assert "WENYI_FAILED" not in capsys.readouterr().out


def test_config_value_error_with_catalog_text_is_not_reported_as_an_upgrade_failure(
    armed_workspace, tmp_path
):
    root, snapshot, _ = armed_workspace
    malformed = tmp_path / "config.yaml"
    malformed.write_text(
        "example: !!int 'not a current-format Desktop catalog'\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="not a current-format Desktop catalog") as caught:
        main.create_context(root, config_path=str(malformed))

    _assert_plain_startup_failure(caught, snapshot)
    assert updates.upgrade_recovery_available(root, ENGINE_TARGET)


def test_newer_catalog_schema_still_requires_upgrade_recovery(armed_workspace, capsys):
    root, snapshot, database = armed_workspace
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA user_version=99")

    with pytest.raises(StartupFailure) as caught:
        main.create_context(root)

    assert caught.value.code == StartupFailureCode.UPGRADE_RECOVERY_REQUIRED
    assert isinstance(caught.value.__cause__, catalog.UnsupportedCatalogFormat)
    # Nothing consumed the prepared snapshot: startup only points at recovery,
    # and the explicit offline command is still the one that runs it.
    assert snapshot.is_dir() and (snapshot / updates.MANIFEST_NAME).is_file()
    assert (root / updates.BACKUPS_DIR / updates.ATTEMPT_NAME).is_file()
    message = capsys.readouterr()
    assert "WENYI_FAILED" not in message.out


def _refuse_with_schema_too_new(monkeypatch):
    """Make construction raise the store's own newer-schema refusal."""

    def refuse(self, *args, **kwargs):
        raise SchemaTooNew("This project was created by a newer version of Wenyi", code="x")

    monkeypatch.setattr(main.LocalBackend, "__init__", refuse)


def test_newer_schema_still_requires_upgrade_recovery_when_a_snapshot_exists(
    armed_workspace, monkeypatch, capsys
):
    root, snapshot, _ = armed_workspace
    _refuse_with_schema_too_new(monkeypatch)

    with pytest.raises(StartupFailure) as caught:
        main.create_context(root)

    # A genuine format refusal must still lead the user to upgrade recovery:
    # the gate is narrowed, not closed.
    assert caught.value.code == StartupFailureCode.UPGRADE_RECOVERY_REQUIRED
    assert snapshot.is_dir() and (snapshot / updates.MANIFEST_NAME).is_file()
    assert "WENYI_FAILED" not in capsys.readouterr().out


def test_newer_schema_without_a_snapshot_still_propagates(tmp_path, monkeypatch):
    monkeypatch.delenv("WENYI_DESKTOP_VERSION", raising=False)
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    catalog.initialize(paths.catalog(root))
    _refuse_with_schema_too_new(monkeypatch)

    # With nothing to roll back to, the refusal is reported as itself rather
    # than turned into recovery guidance that could not be followed.
    with pytest.raises(SchemaTooNew):
        main.create_context(root)


def test_old_format_catalog_still_reports_legacy_data(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("WENYI_DESKTOP_VERSION", raising=False)
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    database = root / "catalog.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('legacy')")
    catalog_bytes = database.read_bytes()

    with pytest.raises(StartupFailure) as caught:
        main.create_context(root)

    # A directory-format refusal keeps its own guidance and rewrites nothing.
    assert caught.value.code == StartupFailureCode.LEGACY_CATALOG
    assert isinstance(caught.value.__cause__, catalog.UnsupportedCatalogFormat)
    assert database.read_bytes() == catalog_bytes
    assert capsys.readouterr().out == ""


def test_old_format_catalog_with_an_armed_snapshot_requires_recovery(armed_workspace, capsys):
    root, snapshot, database = armed_workspace
    database.unlink()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('legacy')")

    with pytest.raises(StartupFailure) as caught:
        main.create_context(root)

    # The card's mapping is kept: an available snapshot wins for a refusal.
    assert caught.value.code == StartupFailureCode.UPGRADE_RECOVERY_REQUIRED
    assert snapshot.is_dir() and (snapshot / updates.MANIFEST_NAME).is_file()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "message", [CATALOG_REFUSAL, f"wrapped: {CATALOG_REFUSAL}"], ids=["exact", "wrapped"]
)
@pytest.mark.parametrize("has_snapshot", [True, False], ids=["with-snapshot", "without-snapshot"])
def test_untyped_catalog_messages_are_not_classified_as_format_refusals(
    armed_workspace, monkeypatch, message, has_snapshot
):
    root, _, _ = armed_workspace
    if not has_snapshot:
        (root / updates.BACKUPS_DIR / updates.ATTEMPT_NAME).unlink()
    error = RuntimeError(message)

    def fail(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(main.LocalBackend, "__init__", fail)
    with pytest.raises(RuntimeError) as caught:
        main.create_context(root)

    assert caught.value is error


def test_writer_catalog_admission_does_not_initialize_an_empty_database(tmp_path):
    database = tmp_path / "catalog.sqlite3"
    sqlite3.connect(database).close()
    before = database.read_bytes()

    with pytest.raises(catalog.UnsupportedCatalogFormat):
        catalog.require_current(database)

    assert database.read_bytes() == before
    catalog.initialize(database)
    catalog.require_current(database)
