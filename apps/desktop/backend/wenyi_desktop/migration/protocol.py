"""The migration contract a version upgrade has to satisfy before it switches layouts.

A snapshot is only trustworthy if it opens and shows the data the old version
committed, and a data directory is only untouched if reading it leaves the files
the old version wrote alone. Both properties are easy to lose while "simplifying"
the read-only open below, so the measured behaviour is written down here.

Measured on this project's own SQLite (3.45.3) against a database created with
the WAL journal mode the application sets in `local_backend.py`:

| how the source is opened                      | result                                        |
|-----------------------------------------------|-----------------------------------------------|
| `file:...?mode=ro`                            | sees every committed row, including the ones  |
|                                               | still sitting in the write-ahead log          |
| `file:...?mode=ro&immutable=1`                | ignores the write-ahead log entirely and       |
|                                               | reports "no such table" for such a database:  |
|                                               | silent data loss in a backup that looks whole |
| a copy of the database file with no log file  | the same missing tables, just as silently     |
| `mode=ro` plus `Connection.backup()`          | correct, log contents included                |
| `mode=ro` plus `VACUUM INTO`                  | correct, log contents included                |

The side effect that decides the design: `mode=ro` stops the connection from
writing *through* itself, but a database in WAL mode still needs a shared-memory
index beside it, so opening one read-only creates its `-shm`/`-wal` companions
when they are not already there. A database in the default rollback journal mode
is opened without touching its directory at all. This application sets WAL
explicitly, so the companions always appear, and "read only" therefore cannot be
allowed to mean "open the workspace where it stands": the old workspace has to
stay exactly as the old version left it until the cleanup batch says otherwise.

Hence `open_read_only()` reads a *copy*: the database and its log file are
copied to a temporary directory, and the connection is opened there. SQLite may
create every companion file it wants; they die with the temporary directory, and
SQLite never touches the workspace at all. Copying the log is what keeps the
newest committed rows visible in that copy.

The copy assumes nothing else is writing, so a caller takes the workspace lock
first. Copying a database that a live writer is still appending to can capture a
log that does not belong to the copied pages.

`immutable=1` is never an option here. The only thing it buys is the absence of
those companions, at the price of a snapshot that opens fine and is missing data.
A backup that looks complete and is not is worse than no backup at all, so the
flag is absent on purpose rather than by oversight.

The five contracts every migration must satisfy are stated on `Migration`.
This module only declares them and provides the read-only open; the layer that
performs a migration implements the protocol and is the only place allowed to
write to the old or the new layout.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Name of the manifest a snapshot writes at its root; the Rust side writes the
#: same file while the engine is stopped, and it is the completion marker.
SNAPSHOT_MANIFEST = "MANIFEST.json"
#: Manifest schema this module understands.
SNAPSHOT_SCHEMA_VERSION = 1
#: Suffix of the write-ahead log. The log holds committed rows that the database
#: file does not, so a read that skips it silently loses the newest data.
LOG_SUFFIX = "-wal"


@dataclass(frozen=True)
class VerifyReport:
    """What a migration found when it compared the new layout with the snapshot.

    An empty `problems` means the migration may switch layouts. Anything else
    means the old layout stays in place and the user is told, because a partial
    migration is not something the application can run on.
    """

    files: int = 0
    total_bytes: int = 0
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the migration produced everything the snapshot promised."""
        return not self.problems


@contextmanager
def open_read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """Read a database without modifying it or the directory that holds it.

    The caller gets a connection to a temporary copy, so it sees the committed
    contents of both the database and its write-ahead log, and the source
    workspace keeps exactly the files it had. See the module docstring for the
    measurement behind that choice, and do not replace it with a direct
    `mode=ro` open or with `immutable=1`.

    The connection is read-only: any statement that writes raises
    `sqlite3.OperationalError`. The copy is discarded when the block exits.
    """
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(database)
    with tempfile.TemporaryDirectory(prefix="wenyi-migration-") as directory:
        copy = Path(directory) / database.name
        shutil.copy2(database, copy)
        log = database.with_name(database.name + LOG_SUFFIX)
        # Only the log is taken along. A shared-memory file describes connections
        # of the process that wrote it, so SQLite rebuilds one for the copy.
        if log.is_file():
            shutil.copy2(log, copy.with_name(copy.name + LOG_SUFFIX))
        connection = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()


@runtime_checkable
class Migration(Protocol):
    """The steps a version upgrade runs, in order, around the layout change.

    A migration is only correct if it satisfies all five:

    1. The old layout is read, never written. Reading uses `open_read_only()`,
       which rules out `immutable=1` for the reason in the module docstring.
    2. Running it twice leaves the same result as running it once.
    3. The new layout is verified before it is used; a migration that cannot
       verify itself must not switch.
    4. Any failure leaves the old layout usable, so a failed upgrade is still a
       working application.
    5. The old data is never deleted. Cleanup is a separate, user-visible action
       with its own conditions, and it is not part of this protocol.
    """

    def preflight(self) -> None:
        """Refuse before anything is written: space, permissions, schema version."""

    def snapshot(self) -> Path:
        """Capture the old layout and return its root.

        The reference implementation is the native snapshot taken before the
        installer runs; a migration may reuse it instead of copying again.
        """

    def apply(self) -> None:
        """Move the data into the new layout. Idempotent, and never destructive."""

    def verify(self) -> VerifyReport:
        """Compare the new layout with the snapshot and report what is missing."""

    def rollback(self) -> None:
        """Return to the old layout after a failed or refused migration."""
