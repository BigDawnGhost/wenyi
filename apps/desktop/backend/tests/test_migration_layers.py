"""The layer-two move: complete, resumable, and invisible to the old workspace.

The acceptance criterion these tests exist for is the one that cannot be
inspected after the fact: the directory the old version wrote has to come out of
a migration with the same files, the same sizes and the same modification times.
A migration can be functionally perfect and still fail that, because reading a
WAL database the obvious way creates its companions beside it.

So every test here takes a fingerprint of the old workspace before the move and
compares it afterwards, including the ones that are about something else.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from wenyi_desktop import paths
from wenyi_desktop.local_backend import LocalBackend
from wenyi_desktop.local_credentials import CredentialStore
from wenyi_desktop.migration import layers
from wenyi_desktop.migration.protocol import LOG_SUFFIX

WORKSPACE = "Wenyi Desktop"


def fingerprint(directory: Path) -> list[tuple[str, int, int, bytes]]:
    """Names, sizes, modification times and bytes, including committed WALs."""
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


class Writer:
    """Build an old-layout workspace the way the previous version left it.

    The databases are created with the WAL journal mode the application sets, and
    the writer is kept open so that the newest rows are still in the log rather
    than in the file. That is the state a migration actually meets, and the state
    in which a bare read-only open would create companions.
    """

    def __init__(self, directory: Path, *, with_credentials: bool = True, keep_open=False):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.database = directory / "local.sqlite3"
        self.connection = sqlite3.connect(self.database)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA wal_autocheckpoint=0")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL UNIQUE, arq_job_id TEXT NOT NULL UNIQUE,
                document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS application_settings (
                id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS progress (
                id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                document TEXT NOT NULL);
        """)
        if with_credentials:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS desktop_credentials "
                "(connection TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS desktop_external_credentials "
                "(service TEXT PRIMARY KEY, mode TEXT NOT NULL, reference TEXT, "
                "storage TEXT, revision INTEGER NOT NULL)"
            )
        self.connection.execute(
            "INSERT INTO projects VALUES (?,?)",
            ("0123456789abcdef", json.dumps({"id": "0123456789abcdef", "name": "Book"})),
        )
        self.connection.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?)",
            (1, "0123456789abcdef", "run-1", "arq-1", json.dumps({"status": "done"})),
        )
        self.connection.execute(
            "INSERT INTO exports VALUES (?,?,?)",
            (1, "0123456789abcdef", json.dumps({"status": "done", "path": "a.epub"})),
        )
        self.connection.execute(
            "INSERT INTO application_settings VALUES (?,?)",
            (1, json.dumps({"template": "标准翻译", "revision": 3, "config": {"language": {}}})),
        )
        self.connection.execute(
            "INSERT INTO progress VALUES (?,?)",
            ("0123456789abcdef", json.dumps({"percent": 40})),
        )
        if with_credentials:
            self.connection.execute(
                "INSERT INTO desktop_credentials VALUES (?,?)",
                ("openai", json.dumps({"mode": "system", "reference": "org.wenyi/1"})),
            )
            self.connection.execute(
                "INSERT INTO desktop_external_credentials VALUES (?,?,?,?,?)",
                ("mineru", "system", "org.wenyi/mineru", "keyring", 2),
            )
        self.connection.commit()
        if not keep_open:
            self.close()

    def close(self) -> None:
        self.connection.close()


@pytest.fixture
def old_workspace(tmp_path):
    """A workspace in the state the previous version leaves behind."""
    writer = Writer(tmp_path / WORKSPACE)
    return writer.directory


def catalog_tables(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        names = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not name.startswith("sqlite_")
        ]
        return {
            name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names
        }
    finally:
        connection.close()


def test_the_old_workspace_is_untouched_byte_for_byte(old_workspace: Path) -> None:
    before = fingerprint(old_workspace)
    layers.migrate(old_workspace)
    # The criterion the whole design exists for. A bare read-only open of a WAL
    # database would add -shm/-wal here and break this assertion on its own.
    assert fingerprint(old_workspace) == before


def test_no_journal_companion_appears_beside_the_old_catalog(tmp_path: Path) -> None:
    writer = Writer(tmp_path / WORKSPACE, keep_open=True)
    try:
        # The row is committed but still in the log, so the log is on disk.
        assert writer.database.with_name(writer.database.name + LOG_SUFFIX).is_file()
        before = fingerprint(writer.directory)
        layers.migrate(writer.directory)
        assert fingerprint(writer.directory) == before
    finally:
        writer.close()


def test_the_move_copies_all_seven_tables(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    counts = catalog_tables(paths.user_catalog())
    # Five owned by LocalBackend, two owned by CredentialStore: one file, two
    # owners, and a move that took only one set would split them.
    for table in layers.TABLES:
        assert table in counts, table
        assert counts[table] >= 1, table
    assert counts["projects"] == 1
    assert counts["jobs"] == 1
    assert counts["exports"] == 1
    assert counts["progress"] == 1
    assert counts["application_settings"] == 1
    assert counts["desktop_credentials"] == 1
    assert counts["desktop_external_credentials"] == 1


def test_the_uniqueness_constraints_survive_the_move(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    database = paths.user_catalog()
    connection = sqlite3.connect(database)
    try:
        # jobs.run_id and jobs.arq_job_id are both UNIQUE, and losing that would
        # let two scheduling records claim one run. The schema is copied rather
        # than re-derived precisely so that this cannot regress silently.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?)",
                (99, "0123456789abcdef", "run-1", "arq-99", "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?)",
                (98, "0123456789abcdef", "run-98", "arq-1", "{}"),
            )
    finally:
        connection.close()


def test_the_foreign_key_to_projects_survives_the_move(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    connection = sqlite3.connect(paths.user_catalog())
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO progress VALUES (?,?)", ("ffffffffffffffff", "{}"))
    finally:
        connection.close()


def test_running_the_move_twice_leaves_the_same_result(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    first = {
        name: path.read_bytes()
        for name in (paths.user_catalog(), paths.user_settings(), paths.user_workspace_index())
        for path in [Path(name)]
    }
    layers.migrate(old_workspace)
    second = {
        name: path.read_bytes()
        for name in (paths.user_catalog(), paths.user_settings(), paths.user_workspace_index())
        for path in [Path(name)]
    }
    assert second == first
    assert catalog_tables(paths.user_catalog())["projects"] == 1


def test_an_interrupted_move_is_resumed_by_the_next_one(old_workspace: Path, monkeypatch) -> None:
    before = fingerprint(old_workspace)
    original = layers.Materializer.index

    def fail(self):
        raise OSError("the disk went away")

    monkeypatch.setattr(layers.Materializer, "index", fail)
    with pytest.raises(OSError):
        layers.migrate(old_workspace)
    # The sentinel records that a move started, so the next start knows to
    # resume instead of trusting a half-built directory.
    assert layers.pending()
    assert not layers.complete(old_workspace)

    monkeypatch.setattr(layers.Materializer, "index", original)
    layers.migrate(old_workspace)
    assert layers.complete(old_workspace)
    assert not layers.pending()
    assert catalog_tables(paths.user_catalog())["desktop_credentials"] == 1
    assert fingerprint(old_workspace) == before


def test_a_half_written_catalog_does_not_survive_an_interruption(
    old_workspace: Path, monkeypatch
) -> None:
    def fail(self):
        raise OSError("the disk went away")

    monkeypatch.setattr(layers.Materializer, "settings", fail)
    with pytest.raises(OSError):
        layers.migrate(old_workspace)
    # The database is published before the documents, so what exists is complete
    # on its own; nothing reports the move as finished.
    assert catalog_tables(paths.user_catalog())["jobs"] == 1
    assert not paths.user_settings().exists()
    assert not layers.complete(old_workspace)


def test_the_sentinel_is_written_before_anything_else(old_workspace: Path, monkeypatch) -> None:
    seen = []
    original = layers.Materializer.database

    def observe(self):
        seen.append(layers.pending())
        return original(self)

    monkeypatch.setattr(layers.Materializer, "database", observe)
    layers.migrate(old_workspace)
    # A crash between the first byte of the new layout and the last one has to
    # leave a marker, or the next start would treat a partial directory as done.
    assert seen == [True]
    assert not layers.pending()


def test_a_second_mover_is_refused_instead_of_waiting(old_workspace: Path) -> None:
    with layers.migration_lock():
        with pytest.raises(layers.MigrationBusy, match="Another Wenyi instance"):
            layers.migrate(old_workspace)


def test_a_running_old_instance_blocks_the_move(old_workspace: Path) -> None:
    from wenyi_core.storage.locks import FileLockHandle

    handle = FileLockHandle(str(paths.native_instance_lock(old_workspace)))
    handle.acquire(blocking=False)
    # Taken after the lock file exists, so the comparison is about what the
    # migration did rather than about the lock this test just created.
    before = fingerprint(old_workspace)
    try:
        with pytest.raises(layers.MigrationBusy, match="previous version"):
            layers.migrate(old_workspace)
    finally:
        handle.release()
    # Refusing means refusing completely: nothing was written and nothing read.
    assert fingerprint(old_workspace) == before
    assert not paths.user_catalog().exists()


def test_a_workspace_without_a_catalog_has_nothing_to_move(tmp_path: Path) -> None:
    workspace = tmp_path / WORKSPACE
    workspace.mkdir()
    assert layers.read(workspace).empty
    assert layers.migrate(workspace) is None
    assert not paths.user_root().exists()


def test_the_settings_table_becomes_a_document(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    import yaml

    document = yaml.safe_load(paths.user_settings().read_text(encoding="utf-8"))
    # The template is not ASCII, so this also proves the document is written as
    # UTF-8 rather than as the machine's locale encoding.
    assert document["template"] == "标准翻译"
    assert document["revision"] == 3
    assert document["schema_version"] == layers.SCHEMA_VERSION


def test_the_project_table_becomes_an_index_of_directories(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    document = json.loads(paths.user_workspace_index().read_text(encoding="utf-8"))
    assert document["schema_version"] == layers.SCHEMA_VERSION
    (project,) = document["projects"]
    assert project["id"] == "0123456789abcdef"
    assert project["name"] == "Book"
    # The directory still belongs to the workspace: layer three owns moving it.
    assert project["path"] == str(paths.project(old_workspace, "0123456789abcdef"))


def test_the_move_records_where_the_data_came_from(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    document = json.loads((paths.user_root() / layers.PROVENANCE).read_text(encoding="utf-8"))
    assert document["source"] == str(paths.catalog(old_workspace))
    assert document["schema_version"] == layers.SCHEMA_VERSION
    assert document["migrated_at"]
    assert document["tables"]["desktop_credentials"] == 1
    assert document["tables"]["projects"] == 1


def test_a_workspace_that_lost_a_catalog_table_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / WORKSPACE
    workspace.mkdir()
    connection = sqlite3.connect(workspace / "local.sqlite3")
    connection.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, document TEXT NOT NULL)")
    connection.commit()
    connection.close()
    with pytest.raises(layers.LayoutError, match="missing"):
        layers.migrate(workspace)


def test_the_two_stores_still_share_one_file_after_the_move(old_workspace: Path) -> None:
    layers.migrate(old_workspace)
    store = CredentialStore(old_workspace)
    backend = LocalBackend(old_workspace)
    try:
        assert store.database == backend.database == paths.user_catalog()
        # Both halves are readable through the one file: the credentials the old
        # version stored and the project it recorded.
        assert backend.get_project("0123456789abcdef")["name"] == "Book"
        assert store.resolve_mineru_token() is None
    finally:
        backend.close()


def test_a_new_workspace_opens_layer_two_without_moving_anything(tmp_path: Path) -> None:
    workspace = tmp_path / "fresh"
    backend = LocalBackend(workspace)
    try:
        assert backend.database == paths.user_catalog()
        assert backend.database.is_file()
        assert catalog_tables(backend.database) == {
            "application_settings": 0,
            "exports": 0,
            "jobs": 0,
            "progress": 0,
            "projects": 0,
        }
    finally:
        backend.close()
    # A fresh installation has no old catalog, and the move writes nothing for
    # it: in particular it must not create a provenance record that claims one.
    assert not layers.complete(workspace)
