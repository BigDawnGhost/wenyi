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

**Re-entry never replaces layer two.** Imported rows and a per-source receipt
commit in one SQLite transaction. A retry repairs only the derived documents,
not rows that may since have changed. A second workspace adds nonconflicting
rows; conflicting identities or credentials refuse the entire transaction.
Existing settings win. The original workspace and its WAL remain untouched.

**Job and export IDs belong to their original catalog.** Each source reserves
an independent integer range in the destination, including retired IDs from
SQLite's sequence. JSON IDs and export-job references follow that mapping;
project/run identities and on-disk export paths do not change. The receipt
records the mapping in the same transaction as the imported rows.

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
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml
from wenyi_core.storage.locks import FileLockHandle, StorageBusyError

from .. import paths
from .ownership import NativeOwner
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
RECEIPTS = "desktop_migrations"
SURROGATE_TABLES = ("jobs", "exports")
# The supported catalog contract, checked before trusting either a receipt or
# an existing destination. CREATE TABLE statements still come from the source.
CATALOG_COLUMNS = {
    "projects": ("id", "document"),
    "jobs": ("id", "project_id", "run_id", "arq_job_id", "document"),
    "exports": ("id", "project_id", "document"),
    "application_settings": ("id", "document"),
    "progress": ("id", "document"),
    "desktop_credentials": ("connection", "document"),
    "desktop_external_credentials": ("service", "mode", "reference", "storage", "revision"),
    RECEIPTS: ("source", "document"),
}


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
    sequences: dict[str, int] = field(default_factory=dict)
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


def _catalog_error(problem: str) -> LayoutError:
    return LayoutError(
        f"Desktop catalog {problem}. Startup was stopped; keep the user catalog and "
        "original workspaces for recovery or manual reconciliation. No data was replaced."
    )


def _record(document: Any, source: str) -> dict[str, Any]:
    """Validate metadata, not current row counts: application data can change."""
    try:
        if isinstance(document, str):
            document = json.loads(document)
        if not isinstance(document, dict):
            raise ValueError
        counts = document["tables"]
        offsets = document.get("id_offsets", dict.fromkeys(SURROGATE_TABLES, 0))
        if (
            not isinstance(source, str)
            or not source
            or type(document["schema_version"]) is not int
            or document["schema_version"] != SCHEMA_VERSION
            or document["source"] != source
            or not isinstance(counts, dict)
            or not set(CATALOG_TABLES).issubset(counts)
            or not set(counts).issubset(TABLES)
            or any(type(count) is not int or count < 0 for count in counts.values())
            or not isinstance(offsets, dict)
            or set(offsets) != set(SURROGATE_TABLES)
            or any(
                type(offset) is not int or not 0 <= offset < 2**63 for offset in offsets.values()
            )
            or datetime.fromisoformat(document["migrated_at"]).tzinfo is None
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise _catalog_error("has an invalid migration receipt") from None
    return document


def _validate_catalog(
    connection: sqlite3.Connection, *, allow_uninitialized: bool = False
) -> dict[str, dict[str, Any]]:
    """Check the durable schema, constraints and receipts in one transaction."""
    tables = set(_tables(connection))
    # CredentialStore may be injected before LocalBackend initializes its tables.
    # This is a fresh state only when no receipt/provenance claims prior adoption.
    initializing = allow_uninitialized and tables.issubset(CREDENTIAL_TABLES)
    missing = set() if initializing else set(CATALOG_TABLES) - tables
    unknown = tables - CATALOG_COLUMNS.keys()
    if missing or unknown:
        raise _catalog_error(
            f"has missing tables {sorted(missing)} or unknown tables {sorted(unknown)}"
        )
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view')"
    ).fetchone():
        raise _catalog_error("has unsupported triggers or views")
    for table in tables:
        info = list(connection.execute(f'PRAGMA table_info("{table}")'))
        if tuple(row[1] for row in info) != CATALOG_COLUMNS[table] or [
            row[1] for row in info if row[5]
        ] != [CATALOG_COLUMNS[table][0]]:
            raise _catalog_error(f"has an unsupported schema for {table}")
        for _, name, kind, required, default, primary in info:
            integer = name == "revision" or (
                name == "id" and table in (*SURROGATE_TABLES, SETTINGS_TABLE)
            )
            nullable = bool(primary) or (
                table == "desktop_external_credentials" and name in ("reference", "storage")
            )
            if (
                kind.upper() != ("INTEGER" if integer else "TEXT")
                or bool(required) != (not nullable)
                or default is not None
            ):
                raise _catalog_error(f"has an unsupported column in {table}")
        if table == SETTINGS_TABLE and "CHECK(ID=1)" not in "".join(
            (_schema(connection, table) or "").upper().split()
        ):
            raise _catalog_error("lost the application settings identity constraint")
        if table in SURROGATE_TABLES:
            if "AUTOINCREMENT" not in (_schema(connection, table) or "").upper():
                raise _catalog_error(f"has an unsupported identity for {table}")
        if table in ("jobs", "exports", "progress"):
            expected = "id" if table == "progress" else "project_id"
            foreign = list(connection.execute(f'PRAGMA foreign_key_list("{table}")'))
            if not any(
                row[2:5] == ("projects", expected, "id") and row[6].upper() == "CASCADE"
                for row in foreign
            ):
                raise _catalog_error(f"lost the project foreign key in {table}")
    unique = {
        tuple(
            row[2] for row in connection.execute("SELECT * FROM pragma_index_info(?)", (index[1],))
        )
        for index in connection.execute("PRAGMA index_list('jobs')")
        if index[2] and not index[4]
    }
    if "jobs" in tables and not {("run_id",), ("arq_job_id",)}.issubset(unique):
        raise _catalog_error("lost a jobs uniqueness constraint")
    if (
        list(connection.execute("PRAGMA quick_check")) != [("ok",)]
        or connection.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise _catalog_error("failed its integrity or foreign-key check")
    receipts = {}
    if RECEIPTS in tables:
        for source, raw in connection.execute(f"SELECT source, document FROM {RECEIPTS}"):
            record = _record(raw, source)
            if not set(record["tables"]).issubset(tables):
                raise _catalog_error("is missing tables promised by a migration receipt")
            receipts[source] = record
    return receipts


def _high_water(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
    return max(
        row[0] if row else 0,
        connection.execute(f'SELECT coalesce(max(id), 0) FROM "{table}"').fetchone()[0],
    )


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
        _validate_catalog(connection)
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
        sequences = {table: _high_water(connection, table) for table in SURROGATE_TABLES}
    return Snapshot(
        source=database,
        tables=tables,
        schema=schema,
        columns=columns,
        rows=rows,
        sequences=sequences,
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
            + " and cannot be moved. Startup was stopped; the original is unchanged."
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
    """Import once transactionally, then repair derived documents on retry."""

    snapshot: Snapshot
    workspace: Path
    home: Path | None = None
    written: list[str] = field(default_factory=list)
    id_offsets: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SURROGATE_TABLES, 0))

    def apply(self) -> None:
        """Write every layer-two entry, then the record that says it finished."""
        self.written.clear()
        self.database()
        self.settings()
        self.index()
        self.provenance()

    def database(self) -> Path:
        """Merge without replacing the database, including its committed WAL."""
        destination = paths.user_catalog(home=self.home)
        records = _provenance_records(self.home)
        if not destination.exists() and records != {}:
            raise _catalog_error(
                "is missing after prior adoption; restore the shared catalog from a backup"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot
        # Reject ambiguous identities before committing any rows from this source.
        self.index_document()
        connection = sqlite3.connect(destination)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            existing = bool(_tables(connection))
            receipts = _validate_catalog(
                connection,
                allow_uninitialized=records == {},
            )
            receipt = receipts.get(str(snapshot.source))
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {RECEIPTS} "
                "(source TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            if receipt is None:
                legacy = records.get(str(snapshot.source)) if existing else None
                if legacy is not None:
                    self.backfill(connection, legacy)
                else:
                    self.merge(connection)
                connection.execute(
                    f"INSERT INTO {RECEIPTS} VALUES (?,?)",
                    (str(snapshot.source), json.dumps(self.record())),
                )
            _validate_catalog(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self.written.append(str(destination))
        return destination

    @cached_property
    def export_projects(self) -> dict[int, str]:
        columns = self.snapshot.columns["exports"]
        return {
            row[columns.index("id")]: row[columns.index("project_id")]
            for row in self.snapshot.rows["exports"]
        }

    def remapped(self, table: str, row: tuple[Any, ...]) -> dict[str, Any]:
        """Remap catalog references, never filesystem paths or domain identities."""
        values = dict(zip(self.snapshot.columns[table], row))
        if table not in SURROGATE_TABLES:
            return values
        old_id = values["id"]
        document = _document(values["document"])
        if type(old_id) is not int or old_id <= 0 or not isinstance(document, dict):
            raise _catalog_error(f"has an invalid {table} record")
        for key in ("id", "project_id", "run_id", "arq_job_id"):
            if key in document and key in values and document[key] != values[key]:
                raise _catalog_error(f"has conflicting {table} document identities")
        values["id"] = old_id + self.id_offsets[table]
        document["id"] = values["id"]
        if table == "jobs" and document.get("kind") == "export":
            params = document.get("params")
            if not isinstance(params, dict) or type(params.get("export_id")) is not int:
                raise _catalog_error("has an invalid export job reference")
            export_id = params["export_id"]
            if export_id <= 0 or export_id > self.snapshot.sequences["exports"]:
                raise _catalog_error("has an invalid export job identity")
            # A retired export may be absent, but a surviving reference cannot
            # point into a different project's history.
            if self.export_projects.get(export_id, values["project_id"]) != values["project_id"]:
                raise _catalog_error("has a cross-project export job reference")
            params["export_id"] = export_id + self.id_offsets["exports"]
        values["document"] = json.dumps(document)
        return values

    def reserve_ids(self, connection: sqlite3.Connection) -> None:
        """Keep retired IDs reserved too, so new exports cannot reuse old paths."""
        for table in SURROGATE_TABLES:
            sequence = self.id_offsets[table] + self.snapshot.sequences[table]
            if connection.execute(
                "SELECT 1 FROM sqlite_sequence WHERE name=?", (table,)
            ).fetchone():
                connection.execute(
                    "UPDATE sqlite_sequence SET seq=max(seq, ?) WHERE name=?", (sequence, table)
                )
            else:
                connection.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, sequence)
                )

    def merge(self, connection: sqlite3.Connection) -> None:
        """Add one source atomically; overlapping domain identities are not retries."""
        for table in TABLES:
            statement = self.snapshot.schema.get(table)
            if statement is not None and _schema(connection, table) is None:
                connection.execute(statement)
        self.id_offsets = {table: _high_water(connection, table) for table in SURROGATE_TABLES}
        for table in TABLES:
            columns = self.snapshot.columns.get(table, ())
            names = ", ".join(f'"{column}"' for column in columns)
            for row in self.snapshot.rows.get(table, ()):
                values = self.remapped(table, row)
                # User-level settings already in use are authoritative.
                if (
                    table == SETTINGS_TABLE
                    and connection.execute(
                        f"SELECT 1 FROM {SETTINGS_TABLE} WHERE id=?", (SETTINGS_ID,)
                    ).fetchone()
                ):
                    continue
                try:
                    connection.execute(
                        f'INSERT INTO "{table}" ({names}) VALUES ({", ".join("?" * len(columns))})',
                        tuple(values.values()),
                    )
                except sqlite3.IntegrityError as error:
                    # Only shared credential references may coincide across
                    # workspaces. A project/run collision needs reconciliation.
                    identical = (
                        table in CREDENTIAL_TABLES
                        and connection.execute(
                            f'SELECT 1 FROM "{table}" WHERE '
                            + " AND ".join(f'"{name}" IS ?' for name in columns),
                            row,
                        ).fetchone()
                    )
                    if not identical:
                        raise _catalog_error(f"has a migration data conflict in {table}") from error
        self.reserve_ids(connection)

    def backfill(self, connection: sqlite3.Connection, record: dict[str, Any]) -> None:
        """Prove a pre-receipt import before recording it, without replaying old data.

        The old implementation kept source IDs. New provenance also carries the
        offsets, allowing a lost receipt to be repaired after a remapped import.
        Missing or mismatched identities are ambiguous (loss versus deletion),
        so they need manual recovery rather than resurrecting stale rows.
        """
        if record["tables"] != {
            table: len(self.snapshot.rows[table]) for table in self.snapshot.tables
        } or not set(self.snapshot.tables).issubset(_tables(connection)):
            raise _catalog_error("cannot reconcile legacy provenance with the original workspace")
        self.id_offsets = record.get("id_offsets", dict.fromkeys(SURROGATE_TABLES, 0))
        for table in ("projects", "jobs", "exports", "progress"):
            for row in self.snapshot.rows[table]:
                values = self.remapped(table, row)
                current = connection.execute(
                    f'SELECT {", ".join(values)} FROM "{table}" WHERE id=?', (values["id"],)
                ).fetchone()
                if current is None:
                    raise _catalog_error(
                        f"cannot reconcile legacy {table}; a recorded row is missing"
                    )
                actual = dict(zip(values, current))
                if any(actual[key] != value for key, value in values.items() if key != "document"):
                    raise _catalog_error(f"has a legacy identity conflict in {table}")
                expected_doc, actual_doc = (
                    _document(values["document"]),
                    _document(actual["document"]),
                )
                if not isinstance(expected_doc, dict) or not isinstance(actual_doc, dict):
                    raise _catalog_error(f"has an invalid legacy document in {table}")
                keys = {
                    "projects": ("id", "created_at"),
                    "jobs": ("id", "project_id", "run_id", "arq_job_id", "kind", "created_at"),
                    "exports": ("id", "project_id", "format", "options", "created_at"),
                    "progress": (),
                }[table]
                if any(
                    key in expected_doc and actual_doc.get(key) != expected_doc[key] for key in keys
                ):
                    raise _catalog_error(f"has a legacy document identity conflict in {table}")
                if table == "jobs" and expected_doc.get("kind") == "export":
                    params = actual_doc.get("params")
                    if (
                        not isinstance(params, dict)
                        or params.get("export_id") != expected_doc["params"]["export_id"]
                    ):
                        raise _catalog_error("has a legacy export reference conflict")
                if (
                    table == "exports"
                    and expected_doc.get("path")
                    and actual_doc.get("path") != expected_doc["path"]
                ):
                    raise _catalog_error("has a legacy export path conflict")
        self.reserve_ids(connection)

    def settings(self) -> Path | None:
        """Write the settings table as the layer-two document.

        The table stays in the catalog as well, because the settings store reads
        it from there; the document is the copy that survives without SQLite,
        which is what the design asks layer two to offer.
        """
        destination = paths.user_settings(home=self.home)
        if destination.exists():
            return destination
        uri = paths.user_catalog(home=self.home).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute(
                f"SELECT document FROM {SETTINGS_TABLE} WHERE id=?", (SETTINGS_ID,)
            ).fetchone()
        document = _document(row[0]) if row else None
        if not isinstance(document, dict):
            return None
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
        document = self.index_document()
        if document is None:
            return None
        destination = paths.user_workspace_index(home=self.home)
        _write_json(destination, document)
        self.written.append(str(destination))
        return destination

    def index_document(self) -> dict[str, Any] | None:
        columns = self.snapshot.columns.get(PROJECT_TABLE, ())
        rows = self.snapshot.rows.get(PROJECT_TABLE, ())
        if not columns or not rows:
            return None
        identity = columns.index("id")
        destination = paths.user_workspace_index(home=self.home)
        existing = (
            json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
        )
        projects = {project["id"]: project for project in existing.get("projects", [])}
        for row in rows:
            document = _document(row[columns.index("document")])
            if not isinstance(document, dict):
                raise LayoutError("a catalog project record is not an object")
            pid = row[identity]
            if document.get("id", pid) != pid:
                raise _catalog_error("has conflicting project document identities")
            path = str(paths.project(self.workspace, pid))
            if pid in projects and projects[pid]["path"] != path:
                raise LayoutError(
                    "Migration project path conflict; original directories are intact"
                )
            projects.setdefault(
                pid,
                {
                    "id": row[identity],
                    "name": document.get("name"),
                    "status": document.get("status"),
                    "created_at": document.get("created_at"),
                    "path": path,
                },
            )
        return {"schema_version": SCHEMA_VERSION, "projects": list(projects.values())}

    def record(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source": str(self.snapshot.source),
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "tables": {
                table: len(self.snapshot.rows.get(table, ()))
                for table in TABLES
                if table in self.snapshot.columns
            },
            "id_offsets": self.id_offsets,
        }

    def provenance(self) -> Path:
        """Record where layer two's data came from and when."""
        destination = paths.user_root(home=self.home) / PROVENANCE
        # Repair stale records, but never erase unreadable shared import history.
        records = _provenance_records(self.home)
        uri = paths.user_catalog(home=self.home).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            for source, document in connection.execute(
                f"SELECT source, document FROM {RECEIPTS} ORDER BY source"
            ):
                records[source] = _record(document, source)
        _write_json(
            destination,
            {**records[str(self.snapshot.source)], "sources": list(records.values())},
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
            connection.execute("BEGIN")
            receipts = _validate_catalog(connection)
            if str(self.snapshot.source) not in receipts:
                problems.append("missing migration receipt")
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


@contextmanager
def native_workspace_lock(workspace: Path, owner: NativeOwner | None) -> Iterator[None]:
    """Hold native admission, or verify the live parent that already holds it."""
    if owner is not None:
        if not owner.valid(workspace):
            raise MigrationBusy("The native Desktop owner could not be verified. Reopen Wenyi.")
        yield
        return
    path = paths.native_instance_lock(workspace)
    if not path.exists():
        yield
        return
    handle = FileLockHandle(str(path))
    try:
        handle.acquire(blocking=False)
    except (StorageBusyError, OSError) as error:
        raise MigrationBusy(
            "The previous version of Wenyi is still running. Close it, then reopen Wenyi."
        ) from error
    try:
        yield
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


def _canonical_record(record: dict[str, Any]) -> dict[str, Any]:
    """Compare receipt fields, excluding the provenance envelope and legacy omissions."""
    return {
        **{key: record[key] for key in ("schema_version", "source", "migrated_at", "tables")},
        "id_offsets": record.get("id_offsets", dict.fromkeys(SURROGATE_TABLES, 0)),
    }


def _provenance_records(home: Path | None) -> dict[str, dict[str, Any]]:
    """Return valid records or absence; refuse unreadable shared import history.

    A single valid receipt cannot prove that other workspaces' receipts survived.
    Replacing damaged provenance with the surviving subset could make those
    workspaces look unimported and resurrect their intentionally deleted data.
    """
    record = provenance(home=home)
    try:
        try:
            raw = record.read_text(encoding="utf-8")
        except FileNotFoundError:
            if not record.is_symlink():
                return {}
            raise
        document = json.loads(raw)
        current = _record(document, document["source"])
        rows = document.get("sources", [document])
        if not isinstance(rows, list) or not rows:
            raise ValueError("Invalid provenance source list")
        records = {row["source"]: _record(row, row["source"]) for row in rows}
        if len(records) != len(rows) or _canonical_record(current) != _canonical_record(
            records[current["source"]]
        ):
            raise ValueError("Inconsistent provenance records")
        return records
    except (OSError, ValueError, KeyError, TypeError, AttributeError, LayoutError):
        raise _catalog_error(
            "has invalid migration provenance; restore it from a consistent backup"
        ) from None


def complete(workspace: Path, *, home: Path | None = None) -> bool:
    """Trust a validated durable receipt, not a surviving provenance document.

    A lost adopted catalog requires backup recovery, not partial reconstruction
    from older originals. A missing receipt needs verified backfill. An
    inconsistent catalog must never be opened for writes.
    The sentinel and derived provenance still require retrying document writes.
    """
    database = paths.user_catalog(home=home)
    source = paths.catalog(workspace)
    records = _provenance_records(home)
    if not database.exists():
        if records != {}:
            raise _catalog_error(
                "is missing after prior adoption; restore the shared catalog from a backup"
            )
        return False
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("BEGIN")
            receipts = _validate_catalog(
                connection,
                # Only pre-completion creation can leave a safely retryable
                # empty file or credentials-only catalog.
                allow_uninitialized=records == {},
            )
    except sqlite3.Error as error:
        raise _catalog_error(
            "could not be read; restore a valid catalog before retrying"
        ) from error
    receipt = receipts.get(str(source))
    if receipt is None:
        if not source.is_file() and (str(source) in records or (not receipts and records)):
            raise _catalog_error("needs its original workspace to verify a missing receipt")
        return False
    record = records.get(str(source))
    return (
        not pending(home=home)
        and record is not None
        and _canonical_record(record) == _canonical_record(receipt)
    )


def check_pending(workspace: Path, *, home: Path | None = None) -> None:
    """Never hide an interrupted import by opening another or a fresh workspace."""
    if not pending(home=home):
        return
    try:
        document = json.loads(sentinel(home=home).read_text(encoding="utf-8"))
        if (
            document["source"] == str(paths.catalog(workspace))
            and paths.catalog(workspace).is_file()
        ):
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise LayoutError(
        "An unfinished Desktop migration needs its original workspace. "
        "Reopen that workspace before starting another; no data was replaced."
    )


def migrate(
    workspace: Path,
    *,
    home: Path | None = None,
    snapshot: Snapshot | None = None,
    native_owner: NativeOwner | None = None,
) -> Snapshot | None:
    """Move one workspace's user-level data into layer two.

    Returns the snapshot that was moved, or `None` when the old layout had
    nothing to move. The workspace is only read. Layer two is written behind the
    migration lock and the sentinel, and the sentinel is removed last, so an
    interruption anywhere in between leaves the next start knowing to resume
    rather than to trust a half-built directory.
    """
    if snapshot is None and not paths.catalog(workspace).is_file():
        # Nothing to move, so nothing is created either: a fresh installation
        # must not find a layer two that claims a move happened.
        return None
    with (
        workspace_lock(workspace),
        native_workspace_lock(workspace, native_owner),
        migration_lock(home=home),
    ):
        check_pending(workspace, home=home)
        if complete(workspace, home=home):
            return None
        current = snapshot if snapshot is not None else read(workspace)
        if current.empty:
            return None
        problems = current.problems()
        if problems:
            raise LayoutError("; ".join(problems))
        marker = sentinel(home=home)
        marker.parent.mkdir(parents=True, exist_ok=True)
        _write_json(marker, {"schema_version": SCHEMA_VERSION, "source": str(current.source)})
        materializer = Materializer(snapshot=current, workspace=workspace, home=home)
        materializer.apply()
        remaining = materializer.verify()
        if remaining:
            raise LayoutError("; ".join(remaining))
        marker.unlink(missing_ok=True)
        return current
