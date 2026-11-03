"""What a start does with the old layout: adopt it once, refusably, and never delete it.

The move itself is covered next door. These tests are about the decision in front
of it, because that decision is what a user actually experiences: the first start
after an upgrade reads their existing data, and every start after that runs on
layer two without touching the old workspace again.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from wenyi_desktop import main, paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.local_credentials import CredentialStore
from wenyi_desktop.migration import layers, startup

WORKSPACE = "Wenyi Desktop"


def fingerprint(directory: Path) -> list[tuple[str, int, int, bytes]]:
    return sorted(
        (
            str(path.relative_to(directory)),
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.read_bytes(),
        )
        for path in directory.rglob("*")
        if path.is_file()
    )


def old_layout(directory: Path) -> Path:
    """A workspace the previous version wrote, in the journal mode it sets."""
    directory.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(directory / "local.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL UNIQUE, arq_job_id TEXT NOT NULL UNIQUE,
            document TEXT NOT NULL);
        CREATE TABLE exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            document TEXT NOT NULL);
        CREATE TABLE application_settings (id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
        CREATE TABLE progress (
            id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            document TEXT NOT NULL);
        CREATE TABLE desktop_credentials (connection TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE desktop_external_credentials (
            service TEXT PRIMARY KEY, mode TEXT NOT NULL, reference TEXT,
            storage TEXT, revision INTEGER NOT NULL);
    """)
    connection.execute(
        "INSERT INTO projects VALUES (?,?)",
        (
            "0123456789abcdef",
            json.dumps(
                {
                    "id": "0123456789abcdef",
                    "name": "Kept",
                    "title": None,
                    "fmt": None,
                    "source_lang": "en",
                    "target_lang": "zh",
                    "status": "done",
                    "created_at": "2026-11-02T10:30:00+00:00",
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO application_settings VALUES (?,?)",
        (1, json.dumps({"template": "标准翻译", "revision": 1, "config": {}})),
    )
    connection.execute(
        "INSERT INTO desktop_credentials VALUES (?,?)",
        ("openai", json.dumps({"mode": "system"})),
    )
    connection.commit()
    connection.close()
    return directory


def add_export_history(workspace: Path, pid: str, run_id: str) -> None:
    """Use the real catalog writers against a synthetic old-layout database."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(paths, "user_catalog", lambda **kwargs: paths.catalog(workspace))
        backend = LocalBackend(workspace)
        try:
            export_id = backend.create_export(pid, "txt", {})
            job_id = backend.create_job(
                pid, "export", run_id, run_id=run_id, params={"export_id": export_id}
            )
            assert export_id == job_id == 1
            output = paths.project(workspace, pid) / "exports" / "1" / "book.txt"
            output.parent.mkdir(parents=True)
            output.write_text("synthetic export", encoding="utf-8")
            backend.set_export_status(
                export_id,
                "done",
                path=output.relative_to(paths.projects(workspace)).as_posix(),
                size=output.stat().st_size,
            )
            backend.set_job_status(job_id, "done")
        finally:
            backend.close()


def test_a_first_start_adopts_the_old_workspace(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    before = fingerprint(workspace)
    report = startup.adopt(workspace)
    assert report.ok, report.problems
    assert paths.user_catalog().is_file()
    assert fingerprint(workspace) == before


def test_a_completed_adoption_is_not_repeated(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    migrated_at = json.loads((paths.user_root() / layers.PROVENANCE).read_text(encoding="utf-8"))[
        "migrated_at"
    ]
    # Every later start has to run on layer two. Re-running the move would be
    # harmless, but it would also mean every start reads a database that may by
    # then be owned by a different, older installation.
    startup.adopt(workspace)
    again = json.loads((paths.user_root() / layers.PROVENANCE).read_text(encoding="utf-8"))[
        "migrated_at"
    ]
    assert again == migrated_at


def test_a_failed_adoption_stops_startup_before_stores_open(tmp_path: Path, monkeypatch) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)

    def fail(self):
        raise OSError("disk full")

    monkeypatch.setattr(layers.Materializer, "database", fail)
    with pytest.raises(layers.LayoutError, match="disk full"):
        main.create_context(workspace)
    # The old workspace is still the complete picture of the user's data.
    assert paths.catalog(workspace).is_file()
    assert not paths.user_catalog().exists()


def test_an_unfinished_adoption_is_resumed_on_the_next_start(tmp_path: Path, monkeypatch) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    original = layers.Materializer.provenance

    def fail(self):
        raise OSError("the machine lost power")

    monkeypatch.setattr(layers.Materializer, "provenance", fail)
    with pytest.raises(layers.LayoutError, match="lost power"):
        startup.adopt(workspace)
    assert layers.pending()
    monkeypatch.setattr(layers.Materializer, "provenance", original)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)


def test_a_refusal_travels_to_the_caller(tmp_path: Path) -> None:
    from wenyi_core.storage.locks import FileLockHandle

    workspace = old_layout(tmp_path / WORKSPACE)
    handle = FileLockHandle(str(paths.native_instance_lock(workspace)))
    handle.acquire(blocking=False)
    try:
        # The old application is still writing the data. This is not a state the
        # new version can wait out, so it is raised rather than reported.
        with pytest.raises(layers.MigrationBusy):
            startup.adopt(workspace)
    finally:
        handle.release()


def test_a_new_installation_writes_nothing_into_layer_two(tmp_path: Path) -> None:
    workspace = tmp_path / "fresh"
    workspace.mkdir()
    report = startup.adopt(workspace)
    assert report.ok
    assert not paths.user_root().exists()


@pytest.mark.parametrize("has_legacy", [False, True])
def test_credentials_can_initialize_layer_two_before_the_catalog(
    tmp_path: Path, has_legacy: bool
) -> None:
    workspace = old_layout(tmp_path / WORKSPACE) if has_legacy else tmp_path / "fresh"
    CredentialStore(workspace)
    assert startup.adopt(workspace).ok
    backend = LocalBackend(workspace)
    try:
        assert bool(backend.list_projects()) == has_legacy
    finally:
        backend.close()


def test_credentials_only_is_not_fresh_when_provenance_survives(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    with sqlite3.connect(paths.user_catalog()) as connection:
        for table in (*layers.CATALOG_TABLES, layers.RECEIPTS):
            connection.execute(f"DROP TABLE {table}")
    before = paths.user_catalog().read_bytes()
    with pytest.raises(layers.LayoutError, match="missing tables"):
        startup.adopt(workspace)
    assert paths.user_catalog().read_bytes() == before


def test_a_started_context_reads_the_migrated_data(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    context = main.create_context(workspace, migrate=False)
    context.repository.close()
    context = main.create_context(workspace)
    try:
        # The project the old version recorded is what the new one lists.
        assert [row["name"] for row in context.repository.list_projects()] == ["Kept"]
        assert context.repository.database == paths.user_catalog()
    finally:
        context.repository.close()


def test_the_old_catalog_is_not_used_again_after_the_move(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    before = fingerprint(workspace)
    context = main.create_context(workspace)
    try:
        context.repository.create_project("New", "en", "zh", {})
    finally:
        context.repository.close()
    # New writes go to layer two only. The old workspace keeps exactly the files
    # it had, which is what makes going back to the previous version work.
    assert fingerprint(workspace) == before


def test_a_second_workspace_is_adopted_even_though_layer_two_is_shared(tmp_path: Path) -> None:
    first = old_layout(tmp_path / "first" / WORKSPACE)
    second = old_layout(tmp_path / "second" / WORKSPACE)
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute(
            "UPDATE projects SET id=?, document=?",
            ("fedcba9876543210", '{"id":"fedcba9876543210","name":"Second"}'),
        )
    add_export_history(first, "0123456789abcdef", "first-run")
    add_export_history(second, "fedcba9876543210", "second-run")
    before = {workspace: fingerprint(workspace) for workspace in (first, second)}
    assert startup.adopt(first).ok
    # Layer two is one directory per machine, so a record that only says "a move
    # happened" is not an answer to "was this workspace moved?". Judging by the
    # record alone would leave the second workspace permanently unadopted.
    assert not layers.complete(second)
    assert startup.adopt(second).ok
    assert layers.complete(second)
    assert layers.complete(first)
    with sqlite3.connect(paths.user_catalog()) as connection:
        assert set(connection.execute("SELECT id FROM projects")) == {
            ("0123456789abcdef",),
            ("fedcba9876543210",),
        }
        jobs = list(connection.execute("SELECT id, project_id, document FROM jobs ORDER BY id"))
        exports = {
            identity: (pid, json.loads(raw))
            for identity, pid, raw in connection.execute(
                "SELECT id, project_id, document FROM exports"
            )
        }
        assert len(jobs) == len(exports) == 2
        for identity, pid, raw in jobs:
            job = json.loads(raw)
            export_pid, export = exports[job["params"]["export_id"]]
            assert job["id"] == identity
            assert export_pid == pid
            assert export["id"] == job["params"]["export_id"]
            # History remains at its old path; remapping must not rename files.
            assert export["path"] == f"{pid}/exports/1/book.txt"
    assert {workspace: fingerprint(workspace) for workspace in (first, second)} == before
    backend = LocalBackend(second)
    try:
        for pid in ("0123456789abcdef", "fedcba9876543210"):
            export = backend.list_exports(pid)[0]
            stream, _ = backend.open_export(pid, export["id"])
            with stream:
                assert stream.read() == b"synthetic export"
        new_export, new_job = backend.admit_export("fedcba9876543210", "txt", {}, "new-run", {})
        assert new_export > max(exports)
        assert new_job > max(identity for identity, _, _ in jobs)
    finally:
        backend.close()
    index = json.loads(paths.user_workspace_index().read_text(encoding="utf-8"))
    assert {project["path"] for project in index["projects"]} == {
        str(paths.project(first, "0123456789abcdef")),
        str(paths.project(second, "fedcba9876543210")),
    }


def test_unreadable_record_stops_startup_until_metadata_is_restored(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    record = layers.provenance().read_bytes()
    layers.provenance().write_text("not json", encoding="utf-8")
    with pytest.raises(layers.LayoutError, match="provenance"):
        layers.complete(workspace)
    with pytest.raises(layers.LayoutError, match="provenance"):
        startup.adopt(workspace)
    layers.provenance().write_bytes(record)
    assert layers.complete(workspace)
    assert startup.adopt(workspace).ok


def test_completed_migration_with_missing_catalog_requires_backup_recovery(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    paths.user_catalog().unlink()
    before = fingerprint(workspace), fingerprint(paths.user_root())
    with pytest.raises(layers.LayoutError, match="restore.*backup|manual reconciliation"):
        layers.complete(workspace)
    with pytest.raises(layers.LayoutError, match="restore.*backup|manual reconciliation"):
        startup.adopt(workspace)
    assert not paths.user_catalog().exists()
    assert (fingerprint(workspace), fingerprint(paths.user_root())) == before


def test_first_adoption_can_resume_after_transaction_rollback(tmp_path: Path, monkeypatch) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    original = layers.Materializer.record
    monkeypatch.setattr(
        layers.Materializer, "record", lambda self: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(layers.LayoutError, match="disk full"):
        startup.adopt(workspace)
    assert layers.pending()
    assert not layers.provenance().exists()
    monkeypatch.setattr(layers.Materializer, "record", original)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)


def test_lost_shared_catalog_cannot_be_partially_rebuilt_from_two_originals(tmp_path: Path) -> None:
    first, second = old_layout(tmp_path / "first"), old_layout(tmp_path / "second")
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute(
            "UPDATE projects SET id=?, document=?",
            ("fedcba9876543210", '{"id":"fedcba9876543210","name":"Second"}'),
        )
    add_export_history(first, "0123456789abcdef", "first-run")
    add_export_history(second, "fedcba9876543210", "second-run")
    assert startup.adopt(first).ok
    assert startup.adopt(second).ok
    backend = LocalBackend(second)
    try:
        backend.admit_export("fedcba9876543210", "txt", {}, "post-migration-run", {})
    finally:
        backend.close()
    paths.user_catalog().unlink()
    before = {path: fingerprint(path) for path in (first, second, paths.user_root())}
    for workspace in (first, second):
        with pytest.raises(layers.LayoutError, match="restore.*backup|manual reconciliation"):
            startup.adopt(workspace)
        assert not paths.user_catalog().exists()
        assert {path: fingerprint(path) for path in before} == before


@pytest.mark.parametrize("invalid", ["not json", "{}", '{"sources":[]}'])
@pytest.mark.parametrize("lost_receipt", [False, True])
def test_invalid_provenance_refuses_replay_even_with_one_valid_receipt(
    tmp_path: Path, invalid: str, lost_receipt: bool
) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    add_export_history(workspace, "0123456789abcdef", "legacy-run")
    startup.adopt(workspace)
    with sqlite3.connect(paths.user_catalog()) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM projects")
        if lost_receipt:
            connection.execute("DELETE FROM desktop_migrations")
    layers.provenance().write_text(invalid, encoding="utf-8")
    before = fingerprint(workspace), fingerprint(paths.user_root())
    database_before = paths.user_catalog().read_bytes()
    with pytest.raises(layers.LayoutError, match="provenance|receipt"):
        startup.adopt(workspace)
    assert (fingerprint(workspace), fingerprint(paths.user_root())) == before
    assert paths.user_catalog().read_bytes() == database_before
    with sqlite3.connect(paths.user_catalog()) as connection:
        for table in ("projects", "jobs", "exports"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_invalid_shared_provenance_cannot_be_erased_by_another_workspace(tmp_path: Path) -> None:
    first, second = old_layout(tmp_path / "first"), old_layout(tmp_path / "second")
    second_id = "fedcba9876543210"
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute(
            "UPDATE projects SET id=?, document=?",
            (second_id, json.dumps({"id": second_id, "name": "Second"})),
        )
    add_export_history(first, "0123456789abcdef", "first-run")
    add_export_history(second, second_id, "second-run")
    startup.adopt(first)
    startup.adopt(second)
    with sqlite3.connect(paths.user_catalog()) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM projects WHERE id=?", (second_id,))
        connection.execute(
            "DELETE FROM desktop_migrations WHERE source=?", (str(paths.catalog(second)),)
        )
    layers.provenance().write_text("damaged shared provenance", encoding="utf-8")
    before = {path: fingerprint(path) for path in (first, second, paths.user_root())}
    for workspace in (first, second):
        # Repairing from just the first receipt would discard evidence of the
        # second import and let a later startup resurrect its deleted history.
        with pytest.raises(layers.LayoutError, match="provenance"):
            startup.adopt(workspace)
        assert {path: fingerprint(path) for path in before} == before
    with sqlite3.connect(paths.user_catalog()) as connection:
        assert connection.execute("SELECT id FROM projects").fetchall() == [("0123456789abcdef",)]
        assert (
            connection.execute(
                "SELECT count(*) FROM exports WHERE project_id=?", (second_id,)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("field", ["tables", "id_offsets", "migrated_at"])
def test_stale_valid_provenance_is_repaired_from_the_transactional_receipt(
    tmp_path: Path, field: str
) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    add_export_history(workspace, "0123456789abcdef", "legacy-run")
    startup.adopt(workspace)
    with sqlite3.connect(paths.user_catalog()) as connection:
        receipt = json.loads(
            connection.execute("SELECT document FROM desktop_migrations").fetchone()[0]
        )
    document = json.loads(layers.provenance().read_text(encoding="utf-8"))
    for record in (document, *document["sources"]):
        if field == "migrated_at":
            record[field] = "2000-01-01T00:00:00+00:00"
        elif field == "tables":
            record[field]["jobs"] += 1
        else:
            record[field]["exports"] += 1
    layers.provenance().write_text(json.dumps(document), encoding="utf-8")
    database_before = paths.user_catalog().read_bytes()
    assert not layers.complete(workspace)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)
    repaired = json.loads(layers.provenance().read_text(encoding="utf-8"))
    assert repaired.pop("sources") == [receipt]
    assert repaired == receipt
    assert paths.user_catalog().read_bytes() == database_before


def test_legacy_missing_offsets_mean_zero_when_comparing_completion_records(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    document = json.loads(layers.provenance().read_text(encoding="utf-8"))
    for record in (document, *document["sources"]):
        record.pop("id_offsets")
    layers.provenance().write_text(json.dumps(document), encoding="utf-8")
    before = fingerprint(paths.user_root())
    assert layers.complete(workspace)
    assert startup.adopt(workspace).ok
    assert fingerprint(paths.user_root()) == before


@pytest.mark.parametrize("missing", ["table", "receipt"])
def test_provenance_does_not_replace_a_transactional_receipt(tmp_path: Path, missing: str) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    with sqlite3.connect(paths.user_catalog()) as connection:
        connection.execute(
            "DROP TABLE desktop_migrations"
            if missing == "table"
            else "DELETE FROM desktop_migrations"
        )
    assert not layers.complete(workspace)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)
    with sqlite3.connect(paths.user_catalog()) as connection:
        assert connection.execute("SELECT count(*) FROM desktop_migrations").fetchone()[0] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "invalid receipt",
        "wrong receipt source",
        "wrong receipt version",
        "invalid receipt counts",
        "invalid receipt offsets",
        "invalid receipt schema",
        "missing projects",
        "missing jobs",
        "missing exports",
        "missing application settings",
        "missing progress",
        "missing credentials",
        "unknown table",
        "lost unique constraint",
    ],
)
def test_inconsistent_catalog_stops_startup_despite_provenance(tmp_path: Path, damage: str) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    with sqlite3.connect(paths.user_catalog()) as connection:
        connection.execute(
            {
                "invalid receipt": "UPDATE desktop_migrations SET document='not json'",
                "wrong receipt source": (
                    "UPDATE desktop_migrations SET document=json_set(document, '$.source', 'other')"
                ),
                "wrong receipt version": (
                    "UPDATE desktop_migrations SET document=json_set(document, '$.schema_version', 99)"
                ),
                "invalid receipt counts": (
                    "UPDATE desktop_migrations SET document=json_set(document, '$.tables.jobs', -1)"
                ),
                "invalid receipt offsets": (
                    "UPDATE desktop_migrations SET document=json_set(document, '$.id_offsets.jobs', -1)"
                ),
                "invalid receipt schema": "ALTER TABLE desktop_migrations RENAME COLUMN source TO path",
                "missing projects": "DROP TABLE projects",
                "missing jobs": "DROP TABLE jobs",
                "missing exports": "DROP TABLE exports",
                "missing application settings": "DROP TABLE application_settings",
                "missing progress": "DROP TABLE progress",
                "missing credentials": "DROP TABLE desktop_credentials",
                "unknown table": "CREATE TABLE unknown_data(value TEXT)",
                "lost unique constraint": "DROP TABLE jobs",
            }[damage]
        )
        if damage == "lost unique constraint":
            connection.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, "
                "run_id TEXT NOT NULL, arq_job_id TEXT NOT NULL UNIQUE, document TEXT NOT NULL)"
            )
    before = paths.user_catalog().read_bytes()
    with pytest.raises(layers.LayoutError, match="catalog|receipt"):
        startup.adopt(workspace)
    assert paths.user_catalog().read_bytes() == before


def test_completion_uses_a_live_sqlite_snapshot_while_another_workspace_writes(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)

    def forbidden_copy(*args, **kwargs):
        raise AssertionError("Completion must not copy live destination DB/WAL files")

    monkeypatch.setattr(layers, "open_read_only", forbidden_copy)
    writer = sqlite3.connect(paths.user_catalog())
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE application_settings SET document=?", ('{"revision":99}',))
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE desktop_migrations SET document='uncommitted'")
        # A reader sees the last committed receipt, without waiting on the writer
        # or accidentally inspecting its uncommitted schema/data.
        assert layers.complete(workspace)
        assert startup.adopt(workspace).ok
        assert startup.adopt(tmp_path / "another-workspace").ok
        writer.rollback()
    finally:
        writer.close()


def test_an_unreadable_destination_is_not_replaced_by_an_empty_catalog(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    database = paths.user_catalog()
    saved = database.read_bytes()
    database.write_bytes(b"synthetic invalid SQLite catalog")
    with pytest.raises(layers.LayoutError, match="restore a valid catalog"):
        startup.adopt(workspace)
    assert database.read_bytes() == b"synthetic invalid SQLite catalog"
    database.write_bytes(saved)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)


def test_retry_after_database_publication_keeps_new_rows_and_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    original = layers.Materializer.settings

    def fail(self):
        raise OSError("interrupted after database publication")

    monkeypatch.setattr(layers.Materializer, "settings", fail)
    with pytest.raises(layers.LayoutError):
        startup.adopt(workspace)
    # Simulate data written by the buggy release after it ignored the failed
    # report, including a committed WAL which has not reached the main file.
    connection = sqlite3.connect(paths.user_catalog())
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO projects VALUES (?,?)", ("eeeeeeeeeeeeeeee", '{"name":"New"}')
        )
        connection.execute("UPDATE desktop_credentials SET document=?", ('{"mode":"environment"}',))
        connection.commit()
        monkeypatch.setattr(layers.Materializer, "settings", original)
        assert startup.adopt(workspace).ok
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 2
        assert connection.execute("SELECT document FROM desktop_credentials").fetchone()[0] == (
            '{"mode":"environment"}'
        )
    finally:
        connection.close()


def test_conflicting_second_workspace_is_refused_without_replacing_any_data(tmp_path: Path) -> None:
    first = old_layout(tmp_path / "first")
    second = old_layout(tmp_path / "second")
    startup.adopt(first)
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute("UPDATE projects SET document=?", ('{"name":"Conflict"}',))
    before = paths.user_catalog().read_bytes()
    with pytest.raises(layers.LayoutError, match="conflict"):
        startup.adopt(second)
    assert paths.user_catalog().read_bytes() == before
    assert not layers.complete(second)


def test_identical_project_ids_in_different_workspaces_cannot_rebind_paths(tmp_path: Path) -> None:
    first = old_layout(tmp_path / "first")
    second = old_layout(tmp_path / "second")
    startup.adopt(first)
    before = paths.user_catalog().read_bytes()
    with pytest.raises(layers.LayoutError, match="path conflict"):
        startup.adopt(second)
    assert paths.user_catalog().read_bytes() == before


def test_second_import_preserves_legacy_completion_and_current_settings(tmp_path: Path) -> None:
    first = old_layout(tmp_path / "first")
    second = old_layout(tmp_path / "second")
    startup.adopt(first)
    # A completed migration from the PR's older implementation has only the
    # provenance file, not the new transactional receipt.
    legacy = json.loads(layers.provenance().read_text(encoding="utf-8"))
    legacy.pop("sources", None)
    legacy.pop("id_offsets")
    layers.provenance().write_text(json.dumps(legacy), encoding="utf-8")
    with sqlite3.connect(paths.user_catalog()) as connection:
        connection.execute("DROP TABLE desktop_migrations")
        connection.execute("UPDATE application_settings SET document=?", ('{"revision":99}',))
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute(
            "UPDATE projects SET id=?, document=?",
            ("fedcba9876543210", '{"id":"fedcba9876543210","name":"Second"}'),
        )
    startup.adopt(second)
    # Importing another workspace cannot certify the unverified legacy record.
    assert not layers.complete(first)
    assert layers.complete(second)
    assert startup.adopt(first).ok
    assert layers.complete(first)
    with sqlite3.connect(paths.user_catalog()) as connection:
        assert connection.execute("SELECT document FROM application_settings").fetchone()[0] == (
            '{"revision":99}'
        )


@pytest.mark.parametrize("remapped", [False, True])
def test_legacy_receipt_backfill_keeps_newer_documents_and_history(
    tmp_path: Path, remapped: bool
) -> None:
    workspace = old_layout(tmp_path / "legacy")
    pid = "0123456789abcdef"
    if remapped:
        earlier = old_layout(tmp_path / "earlier")
        with sqlite3.connect(paths.catalog(earlier)) as connection:
            connection.execute(
                "UPDATE projects SET id=?, document=?",
                ("fedcba9876543210", '{"id":"fedcba9876543210","name":"Earlier"}'),
            )
        add_export_history(earlier, "fedcba9876543210", "earlier-run")
        startup.adopt(earlier)
    add_export_history(workspace, pid, "legacy-run")
    before = fingerprint(workspace)
    startup.adopt(workspace)
    provenance = json.loads(layers.provenance().read_text(encoding="utf-8"))
    if not remapped:
        # The original PR's exact provenance shape, without a mapping or receipt.
        provenance.pop("sources")
        provenance.pop("id_offsets")
        layers.provenance().write_text(json.dumps(provenance), encoding="utf-8")
    with sqlite3.connect(paths.user_catalog()) as connection:
        connection.execute("DROP TABLE desktop_migrations")
        connection.execute(
            "UPDATE projects SET document=json_set(document, '$.name', 'Newer') WHERE id=?", (pid,)
        )
        connection.execute(
            "UPDATE jobs SET document=json_set(document, '$.error', 'Newer') WHERE run_id='legacy-run'"
        )
        connection.execute(
            "UPDATE desktop_credentials SET document=?",
            ('{"mode":"system","reference":"new-credential-reference"}',),
        )
        connection.execute("UPDATE application_settings SET document=?", ('{"revision":99}',))
        connection.execute(
            "INSERT INTO desktop_external_credentials VALUES (?,?,?,?,?)",
            ("mineru", "system", "new-external-reference", "keyring", 3),
        )
    paths.user_settings().write_text("revision: 99\n", encoding="utf-8")
    backend = LocalBackend(workspace)
    try:
        backend.admit_export(pid, "txt", {}, "new-run", {})
    finally:
        backend.close()
    with sqlite3.connect(paths.user_catalog()) as connection:
        before_rows = {
            table: list(connection.execute(f"SELECT * FROM {table}")) for table in layers.TABLES
        }
    assert not layers.complete(workspace)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)
    assert startup.adopt(workspace).ok
    with sqlite3.connect(paths.user_catalog()) as connection:
        assert {
            table: list(connection.execute(f"SELECT * FROM {table}")) for table in layers.TABLES
        } == before_rows
    assert paths.user_settings().read_text(encoding="utf-8") == "revision: 99\n"
    assert fingerprint(workspace) == before


def test_legacy_backfill_refuses_ambiguous_missing_data_and_recovers_after_repair(
    tmp_path: Path,
) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    add_export_history(workspace, "0123456789abcdef", "legacy-run")
    startup.adopt(workspace)
    with sqlite3.connect(paths.user_catalog()) as connection:
        exported = connection.execute("SELECT * FROM exports").fetchone()
        connection.execute("DROP TABLE desktop_migrations")
        connection.execute("DELETE FROM exports")
    before = paths.user_catalog().read_bytes()
    with pytest.raises(layers.LayoutError, match="recorded row is missing"):
        startup.adopt(workspace)
    assert paths.user_catalog().read_bytes() == before
    assert layers.pending()
    with sqlite3.connect(paths.user_catalog()) as connection:
        connection.execute("INSERT INTO exports VALUES (?,?,?)", exported)
    assert startup.adopt(workspace).ok
    assert layers.complete(workspace)
    assert not layers.pending()


@pytest.mark.parametrize("failure", ["record", "settings"])
def test_populated_second_import_is_atomic_and_retry_preserves_new_rows(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    first, second = old_layout(tmp_path / "first"), old_layout(tmp_path / "second")
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute(
            "UPDATE projects SET id=?, document=?",
            ("fedcba9876543210", '{"id":"fedcba9876543210","name":"Second"}'),
        )
    add_export_history(first, "0123456789abcdef", "first-run")
    add_export_history(second, "fedcba9876543210", "second-run")
    # Leave both legacy catalogs with committed rows only in their WALs.
    writers = [sqlite3.connect(paths.catalog(workspace)) for workspace in (first, second)]
    try:
        for writer in writers:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE projects SET document=json_set(document, '$.status', 'done')")
            writer.commit()
        before = {workspace: fingerprint(workspace) for workspace in (first, second)}
        assert startup.adopt(first).ok
        original = getattr(layers.Materializer, failure)

        def fail(self):
            raise OSError("injected interruption")

        monkeypatch.setattr(layers.Materializer, failure, fail)
        with pytest.raises(layers.LayoutError, match="injected interruption"):
            startup.adopt(second)
        with sqlite3.connect(paths.user_catalog()) as connection:
            expected = 1 if failure == "record" else 2
            assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == expected
            assert connection.execute("SELECT count(*) FROM exports").fetchone()[0] == expected
            assert (
                connection.execute("SELECT count(*) FROM desktop_migrations").fetchone()[0]
                == expected
            )
        backend = LocalBackend(first)
        try:
            backend.admit_export("0123456789abcdef", "txt", {}, "new-run", {})
            first_job = backend.get_job_by_arq_id("first-run")
            backend.set_job_status(first_job["id"], "error", "current")
        finally:
            backend.close()
        monkeypatch.setattr(layers.Materializer, failure, original)
        assert startup.adopt(second).ok
        assert startup.adopt(second).ok
        with sqlite3.connect(paths.user_catalog()) as connection:
            assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
            assert connection.execute("SELECT count(*) FROM exports").fetchone()[0] == 3
            assert connection.execute("SELECT count(*) FROM desktop_migrations").fetchone()[0] == 2
            assert connection.execute(
                "SELECT json_extract(document, '$.error') FROM jobs WHERE run_id='first-run'"
            ).fetchone() == ("current",)
            assert (
                list(
                    connection.execute(
                        "SELECT j.project_id, e.project_id FROM jobs j JOIN exports e "
                        "ON json_extract(j.document, '$.params.export_id')=e.id "
                        "WHERE j.project_id != e.project_id"
                    )
                )
                == []
            )
        assert {workspace: fingerprint(workspace) for workspace in (first, second)} == before
    finally:
        for writer in writers:
            writer.close()


@pytest.mark.parametrize("identity", ["run_id", "arq_job_id", "credential"])
def test_real_domain_conflicts_roll_back_the_second_import(tmp_path: Path, identity: str) -> None:
    first, second = old_layout(tmp_path / "first"), old_layout(tmp_path / "second")
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute(
            "UPDATE projects SET id=?, document=?",
            ("fedcba9876543210", '{"id":"fedcba9876543210","name":"Second"}'),
        )
    add_export_history(first, "0123456789abcdef", "first-run")
    add_export_history(second, "fedcba9876543210", "second-run")
    with sqlite3.connect(paths.catalog(second)) as connection:
        if identity == "credential":
            connection.execute(
                "UPDATE desktop_credentials SET document=?", ('{"reference":"other-reference"}',)
            )
        else:
            connection.execute(
                f"UPDATE jobs SET {identity}=?, document=json_set(document, '$.{identity}', ?)",
                ("first-run", "first-run"),
            )
    startup.adopt(first)
    before = paths.user_catalog().read_bytes()
    with pytest.raises(layers.LayoutError, match="conflict"):
        startup.adopt(second)
    assert paths.user_catalog().read_bytes() == before


def test_a_lost_catalog_requires_backup_recovery_even_from_a_fresh_workspace(
    tmp_path: Path,
) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    startup.adopt(workspace)
    paths.user_catalog().unlink()
    before = fingerprint(paths.user_root())
    with pytest.raises(layers.LayoutError, match="restore.*backup|manual reconciliation"):
        startup.adopt(tmp_path / "fresh")
    assert not paths.user_catalog().exists()
    assert fingerprint(paths.user_root()) == before


def test_remapping_reserves_retired_exports_and_their_job_references(tmp_path: Path) -> None:
    first, second = old_layout(tmp_path / "first"), old_layout(tmp_path / "second")
    pid = "fedcba9876543210"
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute(
            "UPDATE projects SET id=?, document=?", (pid, json.dumps({"id": pid, "name": "Second"}))
        )
    add_export_history(first, "0123456789abcdef", "first-run")
    add_export_history(second, pid, "second-run")
    with sqlite3.connect(paths.catalog(second)) as connection:
        connection.execute("INSERT INTO exports VALUES (70, ?, '{}')", (pid,))
        connection.execute("DELETE FROM exports WHERE id=70")
        document = json.loads(connection.execute("SELECT document FROM jobs").fetchone()[0])
        document.update(id=50, run_id="retired-run", arq_job_id="retired-run")
        document["params"]["export_id"] = 70
        connection.execute(
            "INSERT INTO jobs VALUES (50, ?, ?, ?, ?)",
            (pid, "retired-run", "retired-run", json.dumps(document)),
        )
    before = fingerprint(second)
    assert startup.adopt(first).ok
    assert startup.adopt(second).ok
    backend = LocalBackend(second)
    try:
        retired = backend.get_job_by_arq_id("retired-run")
        assert retired["id"] == 51
        assert retired["params"]["export_id"] == 71
        new_export, new_job = backend.admit_export(pid, "txt", {}, "new-run", {})
        assert new_export == 72
        assert new_job == 52
    finally:
        backend.close()
    assert fingerprint(second) == before


def test_fresh_workspace_cannot_ignore_an_incomplete_migration(tmp_path: Path, monkeypatch) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    monkeypatch.setattr(
        layers.Materializer, "settings", lambda self: (_ for _ in ()).throw(OSError("full"))
    )
    with pytest.raises(layers.LayoutError):
        startup.adopt(workspace)
    with pytest.raises(layers.LayoutError, match="unfinished"):
        main.create_context(tmp_path / "fresh")


def test_the_workspace_directory_is_unchanged_even_when_it_had_a_lock(tmp_path: Path) -> None:
    workspace = old_layout(tmp_path / WORKSPACE)
    # A workspace that has been opened once has the engine's lock file. Taking
    # that lock must not create anything new and must not change what is there.
    paths.instance_lock(workspace).write_bytes(b"")
    before = fingerprint(workspace)
    startup.adopt(workspace)
    assert fingerprint(workspace) == before


def test_an_open_workspace_blocks_the_move(tmp_path: Path) -> None:
    from wenyi_core.storage.locks import FileLockHandle

    workspace = old_layout(tmp_path / WORKSPACE)
    path = paths.instance_lock(workspace)
    path.write_bytes(b"")
    handle = FileLockHandle(str(path))
    handle.acquire(blocking=False)
    try:
        with pytest.raises(layers.MigrationBusy, match="open in another window"):
            startup.adopt(workspace)
    finally:
        handle.release()
    assert not paths.user_catalog().exists()
