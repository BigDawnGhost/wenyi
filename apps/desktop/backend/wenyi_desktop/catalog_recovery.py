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
# Reasons raised by an import rather than by the scan itself.
NOT_A_DIRECTORY = "project_path_not_a_directory"
PATH_INVALID = "project_path_invalid"
ALREADY_REGISTERED = "project_already_registered"
DESTINATION_BUSY = "project_destination_exists"
MOVE_FAILED = "project_move_failed"

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
    """The identifier of one project directory, or "" when it is not one.

    The marker is the authority, because a directory can be renamed by hand and
    the readable suffix carries no identity of its own. A directory written by
    the version that had no marker falls back to its name, which is what that
    version used as the whole identity.

    A marker that names a different identifier than its own directory rejects the
    directory rather than choosing one of the two. The two disagreeing means the
    pair was assembled by hand, and rebuilding a catalog entry from it would put
    a project id in front of files that do not belong to it.
    """
    embedded = _marker(directory)
    embedded_id = paths.project_id(str(embedded.get("pid", ""))) if embedded else ""
    named_id, _ = paths.split_project_name(directory.name)
    if embedded_id and named_id and embedded_id != named_id:
        return ""
    return embedded_id or named_id


def _marker(directory: Path) -> dict:
    """Read one project directory's marker, treating an unreadable one as absent.

    Discovery must not stop over a single damaged directory: the rest of the scan
    is still able to rebuild a catalog, and the caller reports this one.
    """
    try:
        document = json.loads((directory / paths.PROJECT_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _metadata(directory: Path) -> dict[str, dict]:
    # Discovery must not initialize a missing DB, change journal mode, or acquire
    # writable schema locks. Read only the small metadata artifacts, including WAL.
    uri = (directory / "state.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
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
    uri = (directory / "state.sqlite3").as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
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

    `strict` decides what an original file that no longer matches the checksum
    means. Automatic recovery passes true and skips such a directory, because a
    catalog it rebuilt from a mismatch would put a project in front of a book it
    never translated. A person who picked the folder passes false: the file is
    the one they mean, so it is accepted and the record is marked, and the next
    parse starts from the new content.

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
        # The original was replaced after the run. Only a directory that holds
        # exactly one possible original can be adopted: with several, nothing in
        # it says which file the saved progress belongs to.
        if strict or len(candidates) != 1:
            return None, SOURCE_CHANGED if strict else SOURCE_AMBIGUOUS
        source, fmt, digest = candidates[0]
        changed = True
    else:
        if len(matches) != 1:
            return None, SOURCE_AMBIGUOUS
        source, fmt, digest = matches[0]
        changed = expected is not None and digest != expected
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
                "source_path": source.relative_to(directory.parent).as_posix(),
                "source_sha256": digest,
                "fmt": fmt,
                "title": title,
                "book_title": title or name,
                "initialized": bool(manifest),
                "status": status,
                "error": error,
                "source_changed": changed,
                "created_at": datetime.fromtimestamp(
                    record.stat().st_mtime, timezone.utc
                ).isoformat(),
            },
        },
        None,
    )


def candidate_roots(backend: LocalBackend) -> list[Path]:
    """Every directory a lost catalog might have recorded projects under.

    The default root is where this installation writes. The other two are places
    a user has already pointed the application at: the library root remembered in
    the settings document, and the absolute paths the layer-two index recorded.
    The index is read for its parent directories rather than for its project
    rows, because a project that moved takes its row with it, while a library
    root usually outlives it.

    The remembered root sits beside the settings `config` rather than inside it:
    `Config` rejects sections it does not know, so a library path stored there
    would make the whole document unreadable. Nothing writes the key yet; the
    batch that adds a library picker owns that, and this only reads it.

    Order matters: the default root is scanned first, so a project that exists in
    both places is recovered where this installation expects it.
    """
    roots = [paths.projects(backend.workspace)]
    saved = backend.load_settings() or {}
    configured = saved.get(paths.LAST_LIBRARY_ROOT)
    if isinstance(configured, str) and configured.strip():
        roots.append(Path(configured))
    roots.extend(_indexed_roots())
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


def _indexed_roots() -> list[Path]:
    """Parent directories of the projects the layer-two index last listed."""
    try:
        document = json.loads(paths.user_workspace_index().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = document.get("projects") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return []
    roots = []
    for entry in entries:
        recorded = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(recorded, str) and recorded.strip():
            roots.append(Path(recorded).parent)
    return roots


def _project_directories(root: Path) -> list[Path]:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if not entry.is_symlink() and entry.is_dir()]


def recover_empty_catalog(backend: LocalBackend) -> None:
    """Called only during Windows startup, after acquiring the workspace process lock."""
    with backend.transaction(write=False) as connection:
        if connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            return
    saved = backend.load_settings()
    defaults = Config.from_dict(saved["config"]) if saved else backend.defaults()
    projects: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for root in candidate_roots(backend):
        if not root.is_dir():
            continue
        for directory in _project_directories(root):
            pid = project_id(directory)
            if not pid or pid in seen:
                continue
            try:
                project, reason = scan_project(directory, strict=True, defaults=defaults)
            except (OSError, sqlite3.Error, ValueError):
                log.warning("Skipping unreadable Desktop project %s", directory.name, exc_info=True)
                continue
            if project is None:
                log.info("Not restoring %s: %s", directory, reason)
                continue
            seen.add(pid)
            projects.append((pid, project))
    if not projects:
        return
    # Hashing and state reads happen outside the short catalog transaction.
    # Commit the complete repair together so a crash can safely retry discovery.
    with backend.transaction() as connection:
        if connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            return
        for pid, project in projects:
            backend.create_project(**project, strategy={}, project_id=pid, connection=connection)


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

    The picked directory is taken at its word about which file is the original.
    The person chose this folder, so a file that changed after the run is
    accepted and flagged for a re-parse rather than refused; automatic recovery
    is the caller that has to stay strict, because nobody is there to confirm.

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
    project, reason = scan_project(directory, strict=False, defaults=defaults or backend.defaults())
    if project is None or reason is not None:
        raise ImportRefused(reason or METADATA_INVALID, _describe(reason))
    # An identifier comes from the marker or the directory name; a folder that
    # carries neither gets a fresh one. The destination is derived from that
    # identifier, because that is how every later path for this project is built.
    pid = project_id(directory) or uuid4().hex[:16]
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

    # The record names the original by its path inside this workspace, which is
    # the directory the rest of the application resolves from.
    project["source"]["source_path"] = (
        destination.relative_to(backend.workspace).as_posix()
        + "/"
        + Path(project["source"]["source_path"]).name
    )
    backend.create_project(**project, strategy={}, project_id=pid)
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
        SOURCE_CHANGED: "The original file no longer matches the saved checksum.",
        PATH_INVALID: "That folder cannot be used.",
        NOT_A_DIRECTORY: "That path is not a folder.",
        ALREADY_REGISTERED: "That project is already in the list.",
        DESTINATION_BUSY: "The library already has a folder for this project.",
        MOVE_FAILED: "The folder could not be moved into the library.",
    }.get(reason or "", "This folder cannot be imported.")
