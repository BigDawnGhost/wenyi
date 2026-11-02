"""The one-time move of an existing workspace's user-level data into layer two.

The old layout keeps a single SQLite file at the workspace root that two
unrelated classes create tables in: `LocalBackend` owns five catalog tables and
`CredentialStore` owns two credential tables. The new layout keeps the same
tables in a file of its own under the user profile and adds the two documents
the design asks for, so this module answers three questions:

1. what does the old file contain, read without changing it;
2. how is the new layout written so that an interrupted run can finish; and
3. what was moved, recorded where a later batch can read it.

**The old workspace is read-only, byte for byte.** That is not a stylistic
preference: the acceptance criterion for this batch is that the directory the old
version wrote keeps exactly the files, names, sizes and modification times it
had. A `?mode=ro` connection to a WAL database creates its `-shm`/`-wal`
companions beside it and would break that criterion on its own, which is why
every read here goes through `open_read_only()`. The measurement behind that
choice is in the docstring of `wenyi_desktop.migration.protocol`; this module
never opens the workspace with `sqlite3.connect` and never renames, moves, or
deletes anything in it. Leaving the old file exactly where the old version left
it is also what makes uninstalling this version and reinstalling that one work.

**The schema is copied, not re-derived.** The tables carry constraints the
application relies on -- `jobs.run_id` and `jobs.arq_job_id` are both unique, and
the catalog's foreign keys are what make a deleted project delete its jobs. A
migration that rebuilt the tables from a list of column names would preserve
every row and silently drop every constraint, so the statements are read out of
the old database's own `sqlite_master` and executed as they were written.

**Re-entry is the normal case, not the exceptional one.** A move that is
interrupted -- by the user, by the operating system, by a full disk -- has to be
able to run again and finish. The new database is therefore built in a temporary
file and published with `os.replace`, the documents are written with the same
temporary-plus-replace pattern this repository uses for manifests, and a sentinel
written before the first byte is what tells the next start to run this module
again instead of assuming the new layout is complete.

**The two halves of one file must not be split.** The catalog tables and the
credential tables lived in one file; a migration that moved one set and not the
other would leave the project list in one database and the provider references in
another. Both sets come from one `open_read_only()` connection and are written
into one new database.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from wenyi_core.storage.locks import FileLockHandle, StorageBusyError

from .. import paths
from .protocol import open_read_only

#: Marker written into layer two while a move is running. It is created before
#: the first byte of the new layout and removed only once every entry it
#: promised exists, so its presence after a crash means "run this again".
SENTINEL = ".migrating"
#: Record of what was moved, when, and from where.
PROVENANCE = ".migrated-from.json"
#: Schema of the documents this module writes.
SCHEMA_VERSION = 1
#: Owned by `LocalBackend`. The order is the order the design lists them in and
#: is the insertion order used to build the new database.
CATALOG_TABLES = ("projects", "jobs", "exports", "application_settings", "progress")
#: Owned by `CredentialStore` in the same file. They only exist once a
#: credential has been stored, so their absence is not an error.
CREDENTIAL_TABLES = ("desktop_credentials", "desktop_external_credentials")
#: Every table the move accounts for.
TABLES = (*CATALOG_TABLES, *CREDENTIAL_TABLES)
#: Table that becomes the settings document as well as staying a table.
SETTINGS_TABLE = "application_settings"
#: Table that becomes the project index as well as staying a table.
PROJECT_TABLE = "projects"
#: Default settings row; the schema keeps exactly one.
SETTINGS_ID = 1


class MigrationBusy(RuntimeError):
    """Another process owns the move, or the old instance still owns the data."""


class LayoutError(RuntimeError):
    """The old layout does not hold what the new one needs."""


@dataclass(frozen=True)
class Snapshot:
    """One complete reading of an old workspace, taken through a temporary copy.

    `schema` holds the original `CREATE TABLE` statement of each table, which is
    what keeps constraints intact. The row tuples are the same rows in the same
    order, read with the columns that statement defines.
    """

    source: Path
    tables: tuple[str, ...] = ()
    schema: dict[str, str] = field(default_factory=dict)
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    rows: dict[str, tuple[tuple[Any, ...], ...]] = field(default_factory=dict)
    settings: Any = None

    @property
    def empty(self) -> bool:
        """Whether the old layout had no catalog to move."""
        return not self.tables

    def problems(self) -> tuple[str, ...]:
        """What stops this reading from being a picture of the whole old layout.

        Two things qualify, and they are reported rather than worked around: a
        table the new layout needs but the old one does not have, and a table
        nobody here knows about. The second is the one that matters -- an
        unknown table is data the move would silently leave behind, so the move
        refuses instead of finishing with a catalog that is quietly incomplete.
        The credential tables are exempt from the first rule because the store
        creates them on first use, so their absence only means no credential was
        ever saved.
        """
        found = [
            f"missing table: {table}"
            for table in (PROJECT_TABLE, SETTINGS_TABLE)
            if table not in self.columns
        ]
        found += [f"unknown table: {table}" for table in self.tables if table not in TABLES]
        return tuple(found)


def _tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return tuple(sorted(name for (name,) in rows if not name.startswith("sqlite_")))


def _schema(connection: sqlite3.Connection, table: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else None


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _read(connection: sqlite3.Connection, table: str) -> tuple[tuple[Any, ...], ...]:
    """Read rows in a stable order, so a resumed move writes identical bytes.

    The order is by every column rather than by a key, because the credential
    tables have a key whose name differs from the catalog's and the settings
    table has no identifier at all. Sorting by all columns is the one order that
    needs no per-table knowledge.
    """
    columns = _columns(connection, table)
    order = ", ".join(f'"{column}"' for column in columns) or "rowid"
    return tuple(connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}').fetchall())


def _snapshot(database: Path) -> Snapshot:
    """Read the catalog through a temporary copy of the database and its log."""
    with open_read_only(database) as connection:
        tables = _tables(connection)
        schema = {}
        columns = {}
        rows = {}
        for table in tables:
            statement = _schema(connection, table)
            if statement is not None:
                schema[table] = statement
            columns[table] = _columns(connection, table)
            rows[table] = _read(connection, table)
        settings = None
        if SETTINGS_TABLE in columns:
            index = columns[SETTINGS_TABLE].index("id")
            document = columns[SETTINGS_TABLE].index("document")
            settings = next(
                (row[document] for row in rows[SETTINGS_TABLE] if row[index] == SETTINGS_ID), None
            )
    return Snapshot(
        source=database,
        tables=tables,
        schema=schema,
        columns=columns,
        rows=rows,
        settings=settings,
    )


def read(workspace: Path) -> Snapshot:
    """Read one workspace's old layout, or report that it has none.

    A workspace without a catalog is not an error: a new installation has none,
    and the caller has nothing to move. Answering with an empty snapshot keeps
    the caller's single question -- is there anything to move -- in one place.
    """
    database = paths.catalog(workspace)
    if not database.is_file():
        return Snapshot(source=database)
    snapshot = _snapshot(database)
    missing = [table for table in CATALOG_TABLES if table not in snapshot.columns]
    if missing:
        raise LayoutError(
            "The existing Desktop catalog is missing "
            + ", ".join(missing)
            + " and cannot be moved. Wenyi will keep using it."
        )
    return snapshot


def _temporary(path: Path) -> Path:
    return path.with_name(path.name + ".migrating")


def _publish(path: Path, write) -> None:
    """Write through a sibling temporary file, then replace the target at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(path)
    temporary.unlink(missing_ok=True)
    try:
        write(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, document: Any) -> None:
    def write(temporary: Path) -> None:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    _publish(path, write)


def _write_yaml(path: Path, document: Any) -> None:
    def write(temporary: Path) -> None:
        temporary.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    _publish(path, write)


@dataclass
class Materializer:
    """Build layer two from one snapshot; safe to run again over the same one.

    Every entry is reconstructed whole and published atomically, so a second run
    produces the same bytes as the first. That is what makes an interrupted move
    resumable rather than merely retryable, and it is why nothing here appends.
    """

    snapshot: Snapshot
    workspace: Path
    home: Path | None = None
    written: list[str] = field(default_factory=list)

    def apply(self) -> None:
        """Write every layer-two entry, then the record that says it finished."""
        self.written.clear()
        self.database()
        self.settings()
        self.index()
        self.provenance()

    def database(self) -> Path:
        """Copy every table, with its own schema, into the layer-two catalog."""
        destination = paths.user_catalog(home=self.home)
        snapshot = self.snapshot

        def write(temporary: Path) -> None:
            connection = sqlite3.connect(temporary)
            try:
                for table in TABLES:
                    statement = snapshot.schema.get(table)
                    if statement is None:
                        continue
                    connection.execute(statement)
                    columns = snapshot.columns.get(table, ())
                    rows = snapshot.rows.get(table, ())
                    if not columns or not rows:
                        continue
                    connection.executemany(
                        f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(columns))})',
                        rows,
                    )
                connection.commit()
            finally:
                connection.close()

        _publish(destination, write)
        self.written.append(str(destination))
        return destination

    def settings(self) -> Path | None:
        """Write the settings table as the layer-two document.

        The table stays in the catalog as well, because the settings store reads
        it from there; the document is the copy that survives without SQLite,
        which is what the design asks layer two to offer.
        """
        document = _document(self.snapshot.settings)
        if not isinstance(document, dict):
            return None
        destination = paths.user_settings(home=self.home)
        _write_yaml(
            destination,
            {
                "schema_version": SCHEMA_VERSION,
                "template": document.get("template"),
                "revision": document.get("revision"),
                "config": document.get("config"),
            },
        )
        self.written.append(str(destination))
        return destination

    def index(self) -> Path | None:
        """Write the project table as the layer-two index of absolute directories.

        The index is not the only record of a project: the catalog keeps the
        rows, and the directories themselves stay where the old version put them
        until the batch that owns layer three moves them.
        """
        columns = self.snapshot.columns.get(PROJECT_TABLE, ())
        rows = self.snapshot.rows.get(PROJECT_TABLE, ())
        if not columns or not rows:
            return None
        identity = columns.index("id")
        projects = []
        for row in rows:
            document = _document(row[columns.index("document")])
            if not isinstance(document, dict):
                raise LayoutError("a catalog project record is not an object")
            projects.append(
                {
                    "id": row[identity],
                    "name": document.get("name"),
                    "status": document.get("status"),
                    "created_at": document.get("created_at"),
                    "path": str(paths.project(self.workspace, row[identity])),
                }
            )
        destination = paths.user_workspace_index(home=self.home)
        _write_json(destination, {"schema_version": SCHEMA_VERSION, "projects": projects})
        self.written.append(str(destination))
        return destination

    def provenance(self) -> Path:
        """Record where layer two's data came from and when."""
        destination = paths.user_root(home=self.home) / PROVENANCE
        _write_json(
            destination,
            {
                "schema_version": SCHEMA_VERSION,
                "source": str(self.snapshot.source),
                "migrated_at": datetime.now(timezone.utc).isoformat(),
                "tables": {
                    table: len(self.snapshot.rows.get(table, ()))
                    for table in TABLES
                    if table in self.snapshot.columns
                },
            },
        )
        self.written.append(str(destination))
        return destination

    def verify(self) -> tuple[str, ...]:
        """Report what layer two is missing, without reading the old layout."""
        database = paths.user_catalog(home=self.home)
        if not database.is_file():
            return (f"missing: {database}",)
        problems: list[str] = []
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            present = set(_tables(connection))
            for table in TABLES:
                if table not in self.snapshot.columns:
                    continue
                if table not in present:
                    problems.append(f"missing table: {table}")
                    continue
                expected = f'PRAGMA table_info("{table}")'
                if (
                    tuple(row[1] for row in connection.execute(expected))
                    != (self.snapshot.columns[table])
                ):
                    problems.append(f"columns differ: {table}")
                    continue
                count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                if count != len(self.snapshot.rows.get(table, ())):
                    problems.append(f"row count differs: {table}")
            if "jobs" in self.snapshot.columns:
                unique = connection.execute(
                    "SELECT count(*) FROM pragma_index_list('jobs') WHERE \"unique\"=1"
                ).fetchone()[0]
                if unique < 2:
                    problems.append("jobs lost a uniqueness constraint")
        finally:
            connection.close()
        for document, expected in (
            (paths.user_settings(home=self.home), self.snapshot.settings is not None),
            (paths.user_workspace_index(home=self.home), bool(self.snapshot.rows.get("projects"))),
        ):
            if expected and not document.is_file():
                problems.append(f"missing: {document}")
        return tuple(problems)


def _document(value: Any) -> Any:
    """Read a stored JSON column the way its writer wrote it."""
    return json.loads(value) if isinstance(value, str) else value


@contextmanager
def migration_lock(home: Path | None = None) -> Iterator[None]:
    """Hold layer two's migration lock, or say that another instance is moving.

    The lock is not waited for: a second instance that blocks here would show a
    frozen window for as long as the first one takes, and the design asks for a
    message instead.
    """
    path = paths.user_migration_lock(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = FileLockHandle(str(path))
    try:
        handle.acquire(blocking=False)
    except (StorageBusyError, OSError) as error:
        raise MigrationBusy(
            "Another Wenyi instance is moving the app data to the new location. "
            "Close it or wait for it to finish, then reopen Wenyi."
        ) from error
    try:
        yield
    finally:
        handle.release()


@contextmanager
def workspace_lock(workspace: Path) -> Iterator[None]:
    """Hold the engine's workspace lock for the duration of the move.

    `open_read_only()` copies a database, and a copy of a database whose writer
    is still running can capture a log that does not belong to the pages beside
    it. Taking the same lock the running application takes is what guarantees
    there is no writer. A lock that is already held means this workspace is open
    somewhere; that is reported rather than waited for, for the same reason the
    migration lock is not waited for.

    The lock file is only opened when it already exists. Opening it is what
    creates it, and creating a file inside the old workspace is exactly what this
    batch promises not to do -- the criterion is that the directory comes out of
    the move with the same entries it had. A workspace without the file has never
    had an engine running against it, so there is no writer to exclude, and the
    absence of the file is itself the proof.
    """
    path = paths.instance_lock(workspace)
    if not path.is_file():
        yield
        return
    handle = FileLockHandle(str(path))
    try:
        handle.acquire(blocking=False)
    except (StorageBusyError, OSError) as error:
        raise MigrationBusy(
            "This Desktop workspace is open in another window. Close it, then reopen Wenyi."
        ) from error
    try:
        yield
    finally:
        handle.release()


def old_instance_running(workspace: Path) -> bool:
    """Whether the previous version still owns this workspace.

    The native lock is probed, never taken over: it belongs to the native parent,
    which owns its lifetime. A workspace whose lock cannot be opened at all is
    reported as busy, because migrating data that a process may still be writing
    is the failure this check exists to prevent.
    """
    path = paths.native_instance_lock(workspace)
    if not path.is_file():
        return False
    handle = FileLockHandle(str(path))
    try:
        handle.acquire(blocking=False)
    except StorageBusyError:
        return True
    except OSError:
        return True
    try:
        return False
    finally:
        handle.release()


def sentinel(home: Path | None = None) -> Path:
    """The in-progress marker inside layer two."""
    return paths.user_storages(home=home) / SENTINEL


def provenance(home: Path | None = None) -> Path:
    """The record of where layer two's data came from."""
    return paths.user_root(home=home) / PROVENANCE


def pending(home: Path | None = None) -> bool:
    """Whether an earlier move started and did not finish."""
    return sentinel(home=home).exists()


def complete(workspace: Path, *, home: Path | None = None) -> bool:
    """Whether layer two has already adopted this particular workspace.

    Layer two is shared by every workspace on the machine, so a provenance record
    is not by itself an answer: it says some workspace was adopted, not that this
    one was. The record names its source for exactly this reason. The sentinel
    decides first, because it is removed last and therefore overrides anything
    else the directory appears to say.
    """
    if pending(home=home):
        return False
    record = provenance(home=home)
    if not record.is_file():
        return False
    try:
        document = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unreadable record cannot be trusted to describe a finished move, so
        # the next start redoes the (idempotent) work rather than assuming one.
        return False
    return str(document.get("source")) == str(paths.catalog(workspace))


def migrate(
    workspace: Path, *, home: Path | None = None, snapshot: Snapshot | None = None
) -> Snapshot | None:
    """Move one workspace's user-level data into layer two.

    Returns the snapshot that was moved, or `None` when the old layout had
    nothing to move. The workspace is only read. Layer two is written behind the
    migration lock and the sentinel, and the sentinel is removed last, so an
    interruption anywhere in between leaves the next start knowing to resume
    rather than to trust a half-built directory.
    """
    if old_instance_running(workspace):
        raise MigrationBusy(
            "The previous version of Wenyi is still running. Close it, then reopen Wenyi."
        )
    if snapshot is None and not paths.catalog(workspace).is_file():
        # Nothing to move, so nothing is created either: a fresh installation
        # must not find a layer two that claims a move happened.
        return None
    with workspace_lock(workspace), migration_lock(home=home):
        current = snapshot if snapshot is not None else read(workspace)
        if current.empty:
            return None
        problems = current.problems()
        if problems:
            raise LayoutError("; ".join(problems))
        marker = sentinel(home=home)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "source": str(current.source)}),
            encoding="utf-8",
        )
        materializer = Materializer(snapshot=current, workspace=workspace, home=home)
        materializer.apply()
        remaining = materializer.verify()
        if remaining:
            raise LayoutError("; ".join(remaining))
        marker.unlink(missing_ok=True)
        return current
