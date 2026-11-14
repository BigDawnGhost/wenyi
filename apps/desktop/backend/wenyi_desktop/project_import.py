"""Explicit imports of the current Desktop project format, never startup discovery."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from wenyi_backend.source_upload import format_for_suffix
from wenyi_core.config import Config
from wenyi_core.ingest.source_hash import source_sha256
from wenyi_core.storage.io_path import require_sqlite_path

from . import paths

if TYPE_CHECKING:
    from .local_backend import LocalBackend

STATE_MISSING = "project_state_missing"
STATE_UNREADABLE = "project_state_unreadable"
METADATA_MISSING = "project_metadata_missing"
METADATA_INVALID = "project_metadata_invalid"
SOURCE_MISSING = "project_source_missing"
SOURCE_AMBIGUOUS = "project_source_ambiguous"
SOURCE_CHANGED = "project_source_changed"
IDENTITY_INVALID = "project_identity_invalid"
PATH_INVALID = "project_path_invalid"
NOT_A_DIRECTORY = "project_path_not_a_directory"
ALREADY_REGISTERED = "project_already_registered"
DESTINATION_BUSY = "project_destination_exists"
MOVE_FAILED = "project_move_failed"


class ImportRefused(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _describe(reason: str | None) -> str:
    return {
        STATE_MISSING: "This folder is not a project: it has no state.sqlite3.",
        STATE_UNREADABLE: "state.sqlite3 cannot be read; it may be damaged.",
        METADATA_MISSING: "state.sqlite3 has no saved book metadata.",
        METADATA_INVALID: "The saved project metadata is not valid.",
        SOURCE_MISSING: "There is no original file beside the project state.",
        SOURCE_AMBIGUOUS: "There is more than one matching original file.",
        SOURCE_CHANGED: "The original file no longer matches the saved checksum.",
        IDENTITY_INVALID: (
            "The project marker is missing, invalid or from an unsupported Desktop format. "
            "Old projects are not migrated."
        ),
        ALREADY_REGISTERED: "That project is already in the list.",
        DESTINATION_BUSY: "The library already has a folder for this project.",
        MOVE_FAILED: "The folder could not be copied into the library.",
    }.get(reason or "", "That folder cannot be imported.")


def scan_project(directory: Path, *, defaults: Config) -> tuple[dict | None, str | None]:
    """Validate the saved source identity without changing project state."""
    state = directory / "state.sqlite3"
    if not state.is_file():
        return None, STATE_MISSING
    try:
        uri = Path(require_sqlite_path(state)).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            rows = connection.execute(
                "SELECT key,value FROM artifacts WHERE key IN (?,?,?)",
                ("manifest.json", "preview.json", "initializing.json"),
            ).fetchall()
        metadata = {key: json.loads(raw) for key, raw in rows}
        if any(not isinstance(value, dict) for value in metadata.values()):
            return None, METADATA_INVALID
    except sqlite3.Error:
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
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return None, METADATA_INVALID
    chapters = manifest.get("chapters", [])
    if not isinstance(chapters, list) or any(not isinstance(ch, dict) for ch in chapters):
        return None, METADATA_INVALID
    candidates = []
    for source in sorted(directory.glob("source-*")):
        fmt = format_for_suffix(source.suffix)
        if fmt and source.is_file() and not source.is_symlink():
            candidates.append((source, fmt, source_sha256(str(source))))
    if not candidates:
        return None, SOURCE_MISSING
    matches = [item for item in candidates if item[2] == expected]
    if not matches:
        return None, SOURCE_CHANGED
    if len(matches) != 1:
        return None, SOURCE_AMBIGUOUS
    source, fmt, digest = matches[0]
    title = manifest.get("title") or preview.get("title")
    title = title if isinstance(title, str) and title.strip() else None
    name = title or f"Imported project {directory.name}"
    status = (
        "done"
        if manifest and chapters and all(ch.get("status") == "done" for ch in chapters)
        else "prepared"
        if manifest
        else "uploaded"
    )
    return {
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
            "error": None,
            "source_changed": False,
            "created_at": datetime.fromtimestamp(state.stat().st_mtime, timezone.utc).isoformat(),
        },
    }, None


def import_project(
    backend: LocalBackend, directory: Path, *, defaults: Config | None = None
) -> dict:
    """Import only a deliberately selected, marked current-format project."""
    if not directory.exists():
        raise ImportRefused(PATH_INVALID, "There is no folder at that location.")
    if not directory.is_dir():
        raise ImportRefused(NOT_A_DIRECTORY, "That path is a file, not a folder.")
    if paths.is_link(directory) or paths.deleting_marker_in(directory).exists():
        raise ImportRefused(PATH_INVALID, "That folder is linked or pending deletion.")
    try:
        # Reject linked metadata before opening SQLite or reading a marker. The
        # copy verification rechecks links in case the folder changes meanwhile.
        for item in directory.rglob("*"):
            if paths.is_link(item):
                raise ImportRefused(PATH_INVALID, "Linked project files cannot be imported.")
        marker = json.loads(paths.marker_in(directory).read_text(encoding="utf-8"))
        if marker.get("schema_version") != paths.PROJECT_SCHEMA_VERSION:
            raise ValueError("Unsupported project format")
        pid = paths.project_id_in(directory)
        if not pid:
            raise ValueError("Missing project identity")
    except (OSError, ValueError, AttributeError) as error:
        raise ImportRefused(IDENTITY_INVALID, _describe(IDENTITY_INVALID)) from error
    project, reason = scan_project(directory, defaults=defaults or backend.defaults())
    if project is None:
        raise ImportRefused(reason or METADATA_INVALID, _describe(reason))
    if backend._get("projects", pid) is not None:
        raise ImportRefused(ALREADY_REGISTERED, _describe(ALREADY_REGISTERED))
    destination = paths.project(backend.workspace, pid)
    if destination.exists():
        raise ImportRefused(DESTINATION_BUSY, _describe(DESTINATION_BUSY))
    destination.parent.mkdir(parents=True, exist_ok=True)
    _adopt_directory(directory, destination)
    backend.create_project(**project, strategy={}, project_id=pid, library_root=destination.parent)
    backend.mark_project(pid, library_root=destination.parent)
    stored = backend.get_project(pid)
    if stored is None:
        raise ImportRefused(STATE_UNREADABLE, "The imported project could not be read back.")
    # Do not remove the explicitly selected original until both files and its
    # catalog row have been published successfully.
    shutil.rmtree(directory, ignore_errors=True)
    return stored


def _file_hashes(root: Path) -> dict[str, str]:
    result = {}
    for item in root.rglob("*"):
        if paths.is_link(item):
            raise ImportRefused(MOVE_FAILED, "Linked project files cannot be imported.")
        if item.is_file():
            result[item.relative_to(root).as_posix()] = source_sha256(str(item))
    return result


def _adopt_directory(directory: Path, destination: Path) -> None:
    staging = destination.with_name(destination.name + ".importing")
    if staging.exists():
        raise ImportRefused(DESTINATION_BUSY, "An incomplete import exists; inspect it first.")
    try:
        expected = _file_hashes(directory)
        shutil.copytree(directory, staging, symlinks=True)
        if _file_hashes(staging) != expected:
            raise ImportRefused(MOVE_FAILED, "The copy does not match the original folder.")
        staging.rename(destination)
    except (OSError, ImportRefused) as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, ImportRefused):
            raise
        raise ImportRefused(MOVE_FAILED, _describe(MOVE_FAILED)) from error
