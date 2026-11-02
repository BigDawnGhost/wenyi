"""Repair an empty Windows catalog without rewriting project state or scheduling work."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from wenyi_backend.source_upload import format_for_suffix
from wenyi_core.config import Config
from wenyi_core.ingest.source_hash import source_sha256

from . import paths

if TYPE_CHECKING:
    from .local_backend import LocalBackend

log = logging.getLogger(__name__)


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


def _project(directory: Path, defaults: Config) -> dict | None:
    metadata = _metadata(directory)
    manifest = metadata.get("manifest.json", {})
    preview = metadata.get("preview.json", {})
    expected = manifest.get("source_sha256") or metadata.get("initializing.json", {}).get(
        "source_sha256"
    )
    if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
        return None
    if manifest and not expected:
        return None
    matches = []
    for source in sorted(directory.glob("source-*")):
        fmt = format_for_suffix(source.suffix)
        if not fmt or not source.is_file() or source.is_symlink():
            continue
        digest = source_sha256(str(source))
        if expected is None or digest == expected:
            matches.append((source, fmt, digest))
    if len(matches) != 1:
        return None
    source, fmt, digest = matches[0]
    title = manifest.get("title") or preview.get("title")
    if not isinstance(title, str) or not title.strip():
        title = None
    name = title
    if not name or re.fullmatch(r"source-[0-9a-f]{32}", name):
        name = f"Recovered project {directory.name}"
    chapters = manifest.get("chapters", [])
    if not isinstance(chapters, list) or any(not isinstance(ch, dict) for ch in chapters):
        return None
    status = "uploaded"
    if manifest:
        # No job history survives catalog loss: offer Start translation rather than
        # a Resume action that requires a lost scheduling record.
        status = (
            "done"
            if chapters and all(ch.get("status") == "done" for ch in chapters)
            else "prepared"
        )
    return {
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
            "created_at": datetime.fromtimestamp(
                (directory / "state.sqlite3").stat().st_mtime, timezone.utc
            ).isoformat(),
        },
    }


def recover_empty_catalog(backend: LocalBackend) -> None:
    """Called only during Windows startup, after acquiring the workspace process lock."""
    with backend.transaction(write=False) as connection:
        if connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone():
            return
    root = paths.projects(backend.workspace)
    if not root.is_dir():
        return
    saved = backend.load_settings()
    defaults = Config.from_dict(saved["config"]) if saved else backend.defaults()
    projects = []
    try:
        directories = sorted(root.iterdir())
    except OSError:
        log.warning("Could not inspect Desktop projects for catalog repair", exc_info=True)
        return
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            continue
        pid = project_id(directory)
        if not pid:
            continue
        try:
            project = _project(directory, defaults)
        except (OSError, sqlite3.Error, ValueError):
            log.warning("Skipping unreadable Desktop project %s", directory.name, exc_info=True)
            continue
        if project:
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
