"""Repair an empty Windows catalog without rewriting project state or scheduling work.

A project directory holds everything a catalog row is a copy of: the source
file, and one `state.sqlite3` whose artifacts carry the manifest. It holds no
scheduling record. Jobs live in the catalog, and the catalog is exactly what a
repair has to rebuild, so the run state a lost catalog would have named is
simply gone. The chapter statuses in the manifest are the only evidence left,
which is why `_status()` falls back to them.

A directory that does carry a `jobs` table -- written by another build of this
application, or placed here by a user who copied one in -- is read when it
exists. That is forward compatibility, not the current format: do not rebuild
this module on the assumption that a project directory records its own jobs.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from wenyi_backend.source_upload import format_for_suffix
from wenyi_core.config import Config
from wenyi_core.ingest.source_hash import source_sha256
from wenyi_core.storage.io_path import require_sqlite_path

from . import paths

if TYPE_CHECKING:
    from .local_backend import LocalBackend

log = logging.getLogger(__name__)

# Reasons a directory does not describe a project this application can restore.
# Each one names the thing that is missing or wrong, because a caller that only
# learns "it failed" cannot tell the user which folder to pick instead.
STATE_MISSING = "project_state_missing"
STATE_UNREADABLE = "project_state_unreadable"
METADATA_MISSING = "project_metadata_missing"
METADATA_INVALID = "project_metadata_invalid"
SOURCE_MISSING = "project_source_missing"
SOURCE_AMBIGUOUS = "project_source_ambiguous"
SOURCE_CHANGED = "project_source_changed"
IDENTITY_INVALID = "project_identity_invalid"
IDENTITY_AMBIGUOUS = "project_identity_ambiguous"
# Reasons raised by an import rather than by the scan itself.
NOT_A_DIRECTORY = "project_path_not_a_directory"
PATH_INVALID = "project_path_invalid"
ALREADY_REGISTERED = "project_already_registered"
DESTINATION_BUSY = "project_destination_exists"
MOVE_FAILED = "project_move_failed"
#: The directory carries a deletion sentinel, so it describes a project that was
#: on its way out when the catalog was lost. Reported rather than adopted.
PROJECT_DELETING = "project_deletion_incomplete"

#: Job state to the project state that admission recorded beside it. A job that
#: is still queued or running is the only evidence of the state a lost catalog
#: cannot otherwise recover: the chapters alone cannot distinguish "stopped
#: between two chapters" from "stopped in the middle of one".
_IN_FLIGHT = {
    "parse": "parsing",
    "prepare": "preparing",
    "translation": "translating",
    "chapter_translation": "translating",
    "review": "reviewing",
}


def project_id(directory: Path) -> str:
    """Read a valid identity, or "" for an unmarked name with no identifier.

    The marker is the authority, because a directory can be renamed by hand and
    the readable suffix carries no identity of its own. A directory written by
    the version that had no marker falls back to its name, which is what that
    version used as the whole identity.

    A malformed marker or one that contradicts its directory raises, rather than
    silently assigning a different identity during recovery or import.
    """
    return paths.project_id_in(directory)


def _state_uri(directory: Path) -> str:
    """Read-only SQLite URI for one project directory, refusing unopenable paths.

    Discovery must not initialize a missing DB, change journal mode, or acquire
    writable schema locks. SQLite also rejects the extended-length prefix, so a
    path too long to open is reported here rather than as a URI failure.
    """
    return Path(require_sqlite_path(directory / "state.sqlite3")).as_uri() + "?mode=ro"


def _metadata(directory: Path) -> dict[str, dict]:
    with closing(sqlite3.connect(_state_uri(directory), uri=True)) as connection:
        rows = connection.execute(
            "SELECT key,value FROM artifacts WHERE key IN (?,?,?)",
            ("manifest.json", "preview.json", "initializing.json"),
        ).fetchall()
    result = {}
    for key, raw in rows:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid project metadata")
        result[key] = value
    return result


def _newest_job(directory: Path) -> dict | None:
    """The last scheduling record this directory kept, if it kept one.

    A catalog is not the only place a run leaves a trace. A directory that was
    written by a version which also recorded jobs beside the project state can
    say what the run was doing when the catalog was lost, and that is the
    difference between offering "continue" and offering "start over".

    Directories written by the current engine hold no such table, so its absence
    is the ordinary case rather than a fault: the caller falls back to what the
    chapter statuses can prove.
    """
    try:
        with closing(sqlite3.connect(_state_uri(directory), uri=True)) as connection:
            row = connection.execute(
                "SELECT kind, status, error FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    kind, status, error = row
    if not isinstance(kind, str) or not isinstance(status, str):
        return None
    return {"kind": kind, "status": status, "error": error if isinstance(error, str) else None}


def _status(manifest: dict, chapters: list, job: dict | None) -> tuple[str, str | None]:
    """The state a project directory is in, and the failure it stopped on."""
    if job is not None:
        if job["status"] in {"queued", "running"}:
            running = _IN_FLIGHT.get(job["kind"])
            if running:
                return running, None
        if job["status"] in {"paused", "interrupted"}:
            return "paused", job["error"]
        if job["status"] == "error":
            return "error", job["error"]
        # A finished job describes a run that ended, so the chapters decide.
    if not manifest:
        # No job history survives catalog loss: offer Start translation rather than
        # a Resume action that requires a lost scheduling record.
        return "uploaded", None
    if chapters and all(chapter.get("status") == "done" for chapter in chapters):
        return "done", None
    return "prepared", None


def _sources(directory: Path) -> list[tuple[Path, str, str]]:
    """Every file in the directory that could be the project's original."""
    found = []
    for source in sorted(directory.glob("source-*")):
        fmt = format_for_suffix(source.suffix)
        if not fmt or not source.is_file() or source.is_symlink():
            continue
        found.append((source, fmt, source_sha256(str(source))))
    return found


def scan_project(
    directory: Path, *, strict: bool, defaults: Config
) -> tuple[dict | None, str | None]:
    """Rebuild one catalog record from a directory, or say what is wrong with it.

    Both automatic recovery and explicit import preserve saved source identity.
    Picking a folder is not permission to attach different input bytes to its
    translations. `strict` controls only how ambiguous mismatches are reported.

    The failure is returned rather than logged so a caller can tell the user
    which part of the directory is missing or damaged.
    """
    record = directory / "state.sqlite3"
    if not record.is_file():
        return None, STATE_MISSING
    try:
        metadata = _metadata(directory)
    except sqlite3.DatabaseError:
        return None, STATE_UNREADABLE
    except (OSError, ValueError):
        return None, METADATA_INVALID
    manifest = metadata.get("manifest.json", {})
    preview = metadata.get("preview.json", {})
    if not manifest and not preview and not metadata.get("initializing.json"):
        return None, METADATA_MISSING
    expected = manifest.get("source_sha256") or metadata.get("initializing.json", {}).get(
        "source_sha256"
    )
    if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
        return None, METADATA_INVALID
    if manifest and not expected:
        return None, METADATA_INVALID
    chapters = manifest.get("chapters", [])
    if not isinstance(chapters, list) or any(not isinstance(ch, dict) for ch in chapters):
        return None, METADATA_INVALID
    candidates = _sources(directory)
    if not candidates:
        return None, SOURCE_MISSING
    matches = [item for item in candidates if expected is None or item[2] == expected]
    if not matches:
        return None, SOURCE_CHANGED if strict or len(candidates) == 1 else SOURCE_AMBIGUOUS
    if len(matches) != 1:
        return None, SOURCE_AMBIGUOUS
    source, fmt, digest = matches[0]
    title = manifest.get("title") or preview.get("title")
    if not isinstance(title, str) or not title.strip():
        title = None
    name = title
    if not name or re.fullmatch(r"source-[0-9a-f]{32}", name):
        name = f"Recovered project {directory.name}"
    try:
        job = _newest_job(directory)
    except sqlite3.DatabaseError:
        job = None
    status, error = _status(manifest, chapters, job)
    return (
        {
            "name": name,
            "source_lang": manifest.get("source_lang") or preview.get("source_lang") or "auto",
            "target_lang": manifest.get("target_lang") or defaults.target_lang,
            "source": {
                "source_path": source.name,
                "source_sha256": digest,
                "fmt": fmt,
                "title": title,
                "book_title": title or name,
                "initialized": bool(manifest),
                "status": status,
                "error": error,
                "source_changed": False,
                "created_at": datetime.fromtimestamp(
                    record.stat().st_mtime, timezone.utc
                ).isoformat(),
            },
        },
        None,
    )


def candidate_roots(backend: LocalBackend) -> list[Path]:
    """Scan the same durable roots that normal lookup and deletion admit."""
    roots = [paths.projects(backend.workspace), *backend.project_roots()]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except (OSError, RuntimeError):
            continue
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _project_directories(root: Path) -> list[Path]:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if not entry.is_symlink() and entry.is_dir()]


def recover_empty_catalog(backend: LocalBackend) -> RecoveryReport:
    """Called only during Windows startup, after acquiring the workspace process lock.

    Returns what the scan decided, so startup can tell the user that some
    folders were not adopted instead of leaving them silently absent from a
    list that looks complete.
    """
    with backend.transaction(write=False) as connection:
        if connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            return RecoveryReport()
    saved = backend.load_settings()
    defaults = Config.from_dict(saved["config"]) if saved else backend.defaults()
    projects: list[tuple[str, Path, dict]] = []
    seen: dict[str, Path] = {}
    ambiguous: set[str] = set()
    skipped: list[tuple[Path, str]] = []
    for root in candidate_roots(backend):
        if not root.is_dir():
            continue
        for directory in _project_directories(root):
            try:
                pid = project_id(directory)
            except (OSError, ValueError):
                skipped.append((directory, IDENTITY_INVALID))
                continue
            if not pid:
                continue
            if pid in seen:
                # Nothing is published until discovery finishes. Retract the
                # earlier candidate and report every copy, even if one had
                # already failed its metadata scan.
                if pid not in ambiguous:
                    previous = seen[pid]
                    projects = [item for item in projects if item[0] != pid]
                    skipped = [item for item in skipped if item[0] != previous]
                    skipped.append((previous, IDENTITY_AMBIGUOUS))
                    ambiguous.add(pid)
                skipped.append((directory, IDENTITY_AMBIGUOUS))
                continue
            seen[pid] = directory
            if paths.deleting_marker_in(directory).is_file():
                # The person asked for this project to be deleted and the
                # deletion was interrupted. Adopting it again would undo the
                # request, and finishing the deletion here would run a
                # recursive removal behind a startup the user did not ask for.
                # It is reported instead, and a later explicit removal finishes
                # it: the sentinel and the directory are left exactly as found.
                log.info("Not restoring %s: deletion was already requested", directory)
                skipped.append((directory, PROJECT_DELETING))
                continue
            try:
                project, reason = scan_project(directory, strict=True, defaults=defaults)
            except (OSError, sqlite3.Error, ValueError):
                log.warning("Skipping unreadable Desktop project %s", directory.name, exc_info=True)
                skipped.append((directory, STATE_UNREADABLE))
                continue
            if project is None:
                log.info("Not restoring %s: %s", directory, reason)
                skipped.append((directory, reason or METADATA_INVALID))
                continue
            projects.append((pid, directory, project))
    if not projects:
        return RecoveryReport(skipped=skipped)
    # Hashing and state reads happen outside the short catalog transaction.
    # Commit the complete repair together so a crash can safely retry discovery.
    with backend.transaction() as connection:
        if connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            # Another instance completed the repair while this one was scanning.
            return RecoveryReport()
        for pid, directory, project in projects:
            backend.create_project(
                **project,
                strategy={},
                project_id=pid,
                library_root=directory.parent,
                connection=connection,
            )
    return RecoveryReport(recovered=len(projects), skipped=skipped)


class RecoveryReport:
    """What an automatic catalog repair decided, for the person who lost a catalog.

    A repair that silently adopts three folders out of five is indistinguishable
    from one that found three, and the difference is the whole question a user
    asks: "where did my project go?". The skipped folders and their reasons are
    therefore carried out of the scan, not only written to a log the user cannot
    see. Reasons are the same codes the import refusal uses, so a startup notice
    and a refused folder speak one vocabulary.
    """

    def __init__(
        self,
        recovered: int = 0,
        skipped: list[tuple[Path, str]] | None = None,
    ) -> None:
        self.recovered = recovered
        self.skipped = skipped or []

    def __bool__(self) -> bool:
        return bool(self.recovered or self.skipped)

    def describe(self) -> str:
        """The sentence a person reads: what came back, and what did not."""
        parts = [f"Recovered {self.recovered} project(s)."] if self.recovered else []
        if self.skipped:
            parts.append(f"Skipped {len(self.skipped)} folder(s):")
            parts.extend(
                f"\n- {directory.name}: {_describe(reason)}" for directory, reason in self.skipped
            )
        return " ".join(parts).replace(": \n", ":\n")

    def as_dict(self) -> dict:
        """The same answer for a caller that renders it rather than prints it."""
        return {
            "recovered": self.recovered,
            "skipped": [
                {
                    "path": str(directory),
                    "name": directory.name,
                    "reason": reason,
                    "message": _describe(reason),
                }
                for directory, reason in self.skipped
            ],
        }


class ImportRefused(Exception):
    """A directory the user picked that this application will not adopt.

    The reason travels with the message because the interface shows it verbatim:
    "the folder has no state.sqlite3" is actionable, "import failed" is not.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def import_project(
    backend: LocalBackend, directory: Path, *, defaults: Config | None = None
) -> dict:
    """Adopt one directory the user picked, or refuse and say why.

    The original must match the saved state before any copying or removal. A
    changed original belongs in a new project, never over existing translations.

    The directory is moved into this workspace before it is recorded, because
    the rest of the application opens a project through `paths.project()` and
    nothing else. Refusals raise `ImportRefused` so the route can answer with a
    specific reason instead of a bare failure.
    """
    if not directory.exists():
        raise ImportRefused(PATH_INVALID, f"There is no folder at {directory}.")
    if not directory.is_dir():
        raise ImportRefused(NOT_A_DIRECTORY, f"{directory.name} is a file, not a folder.")
    if directory.is_symlink():
        raise ImportRefused(PATH_INVALID, f"{directory.name} is a link, not a folder.")
    try:
        identity = project_id(directory)
    except (OSError, ValueError) as error:
        raise ImportRefused(IDENTITY_INVALID, _describe(IDENTITY_INVALID)) from error
    project, reason = scan_project(directory, strict=False, defaults=defaults or backend.defaults())
    if project is None or reason is not None:
        raise ImportRefused(reason or METADATA_INVALID, _describe(reason))
    # An identifier comes from the marker or the directory name; a folder that
    # carries neither gets a fresh one. The destination is derived from that
    # identifier, because that is how every later path for this project is built.
    pid = identity or uuid4().hex[:16]
    try:
        with backend.transaction(write=False) as connection:
            recorded = connection.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone()
    except sqlite3.Error as error:
        raise ImportRefused(STATE_UNREADABLE, "The project catalog could not be read.") from error
    if recorded:
        raise ImportRefused(ALREADY_REGISTERED, f"{project['name']} is already in the list.")
    destination = paths.project(backend.workspace, pid)
    if destination.exists():
        raise ImportRefused(DESTINATION_BUSY, f"{destination.name} is already in the library.")
    if directory.resolve() != destination.resolve():
        destination.parent.mkdir(parents=True, exist_ok=True)
        _adopt_directory(directory, destination)

    # Source references stay local to the directory after import, independently
    # of the workspace layout or the library selected for new projects.
    backend.create_project(**project, strategy={}, project_id=pid, library_root=destination.parent)
    backend.mark_project(pid, library_root=destination.parent)
    stored = backend.get_project(pid)
    if stored is None:
        raise ImportRefused(STATE_UNREADABLE, "The imported project could not be read back.")
    return stored


def _adopt_directory(directory: Path, destination: Path) -> None:
    """Move one picked folder into the library without ever risking the original.

    `shutil.move` is not safe for this. Across volumes it copies file by file and
    unlinks the source as it goes, so a failure halfway (a full disk, a permission
    error on one file, a removed drive) leaves a partial copy in the library *and*
    a source that is already missing the files that were moved. The user picked
    this folder precisely because it is the only copy of their work.

    So the copy, the proof and the removal are three separate steps: copy into a
    sibling temporary directory, verify the copy against the source, publish it
    with one rename, and only then remove the original. A failure at any point
    before the rename leaves the source untouched; a failure after it leaves a
    complete library copy and a source that can still be removed by hand.
    """
    staging = destination.with_name(destination.name + ".importing")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(directory, staging, symlinks=True)
        _verify_copy(directory, staging)
        staging.rename(destination)
    except OSError as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise ImportRefused(
            MOVE_FAILED, f"Could not copy the folder into the library: {error}"
        ) from error
    except ImportRefused:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # The library copy is complete and published; the original is now redundant.
    # A failure here is not fatal: the project works, and the leftover folder is
    # something the user can remove. Reporting it as a failed import would be a
    # lie about a project that was in fact imported.
    shutil.rmtree(directory, ignore_errors=True)


def _verify_copy(source: Path, copy: Path) -> None:
    """Refuse a copy whose file set or sizes disagree with the original."""
    expected = {
        item.relative_to(source).as_posix(): item.stat().st_size
        for item in source.rglob("*")
        if item.is_file()
    }
    actual = {
        item.relative_to(copy).as_posix(): item.stat().st_size
        for item in copy.rglob("*")
        if item.is_file()
    }
    if expected != actual:
        raise ImportRefused(MOVE_FAILED, "The copy does not match the original folder.")


def _describe(reason: str | None) -> str:
    """The sentence a person reads for one scan failure."""
    return {
        STATE_MISSING: "This folder is not a project: it has no state.sqlite3.",
        STATE_UNREADABLE: "state.sqlite3 cannot be read; it may be damaged or from another version.",
        METADATA_MISSING: "state.sqlite3 has no manifest, so it records no book to restore.",
        METADATA_INVALID: "The saved project metadata is not valid.",
        SOURCE_MISSING: "There is no original file beside the project state.",
        SOURCE_AMBIGUOUS: "There is more than one possible original file, and none matches.",
        SOURCE_CHANGED: (
            "The original file no longer matches the saved checksum. "
            "Restore the matching original or create a new project for the changed text."
        ),
        IDENTITY_INVALID: (
            "The project marker is unreadable, invalid, or contradicts the directory identifier."
        ),
        IDENTITY_AMBIGUOUS: (
            "Multiple folders claim the same project identity. "
            "Choose the intended copy explicitly instead of automatic recovery."
        ),
        PATH_INVALID: "That folder cannot be used.",
        NOT_A_DIRECTORY: "That path is not a folder.",
        ALREADY_REGISTERED: "That project is already in the list.",
        DESTINATION_BUSY: "The library already has a folder for this project.",
        MOVE_FAILED: "The folder could not be moved into the library.",
    }.get(reason or "", "This folder cannot be imported.")
