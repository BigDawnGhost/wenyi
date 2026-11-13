"""Desktop paths derived exclusively from the app's explicit data root.

The catalog owns settings, scheduling and opaque credential references. Project
directories own domain state. No resolver searches a previous layout or uses an
installation directory to decide where data belongs.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

from wenyi_core.storage.run_store import slugify

LAST_LIBRARY_ROOT = "last_library_root"
LIBRARY_ROOTS = "library_roots"
PROJECT_MARKER = "project.json"
PROJECT_SCHEMA_VERSION = 2
PROJECT_DELETING = ".deleting"
_PROJECT_ID = re.compile(r"[0-9a-f]{16}")


def is_link(path: Path) -> bool:
    """Include Windows junctions/reparse points, not only POSIX-style symlinks."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def catalog(workspace: Path) -> Path:
    """One catalog for this root, including settings and credential references."""
    return workspace / "catalog.sqlite3"


def projects(workspace: Path) -> Path:
    return workspace / "projects"


def project(workspace: Path, pid: str) -> Path:
    return projects(workspace) / pid


def project_in(root: Path, pid: str, name: str) -> Path:
    return root / project_name(pid, name)


def project_marker(workspace: Path, pid: str) -> Path:
    return marker_in(project(workspace, pid))


def marker_in(directory: Path) -> Path:
    return directory / PROJECT_MARKER


def deleting_marker_in(directory: Path) -> Path:
    return directory / PROJECT_DELETING


def runtime_root(workspace: Path) -> Path:
    return workspace / "runtime"


def plugins(workspace: Path) -> Path:
    return runtime_root(workspace) / "plugins" / "babeldoc"


def diagnostics(workspace: Path) -> Path:
    return workspace / "logs"


def locks(workspace: Path) -> Path:
    return workspace / "locks"


def instance_lock(workspace: Path) -> Path:
    return locks(workspace) / ".desktop.lock"


def native_instance_lock(workspace: Path) -> Path:
    return locks(workspace) / ".desktop-instance.lock"


def instance_endpoint(workspace: Path) -> Path:
    return locks(workspace) / ".desktop-instance.json"


def platform_home() -> Path:
    """Resolve an absolute profile for rejecting unsafe project locations."""
    name = "USERPROFILE" if sys.platform == "win32" else "HOME"
    configured = Path(os.environ.get(name, "").strip())
    if configured.is_absolute():
        return configured
    fallback = Path.home()
    if fallback.is_absolute():
        return fallback
    raise RuntimeError("Wenyi could not determine your user folder.")


def project_id(value: str) -> str:
    return value if _PROJECT_ID.fullmatch(value) else ""


def project_name(pid: str, name: str) -> str:
    """Stable identity first; bound the readable suffix for Windows path lengths."""
    return f"{pid}-{slugify(name)[:64]}"


def split_project_name(name: str) -> tuple[str, str]:
    pid = project_id(name[:16])
    if not pid:
        return "", ""
    if len(name) == 16:
        return pid, ""
    if name[16] != "-":
        return "", ""
    return pid, name[17:]


def _project_marker_id(directory: Path) -> str:
    try:
        marker = json.loads(marker_in(directory).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    if not isinstance(marker, dict):
        raise ValueError("Invalid project marker")
    value = marker.get("pid")
    if not isinstance(value, str) or not project_id(value):
        raise ValueError("Invalid project identifier in marker")
    if marker.get("schema_version") != PROJECT_SCHEMA_VERSION:
        raise ValueError("Unsupported Desktop project format; old projects are not migrated")
    return value


def project_id_in(directory: Path) -> str:
    embedded = _project_marker_id(directory)
    named, _ = split_project_name(directory.name)
    if embedded and named and embedded != named:
        raise ValueError("Project directory does not belong to this project")
    return embedded or named


def find_project(root: Path, pid: str) -> Path | None:
    """Find one identity only within its recorded root; refuse duplicates and links."""
    return ProjectDirectoryIndex(root).find(pid)


class ProjectDirectoryIndex:
    """One read's library inventory, never an authority for subsequent operations."""

    def __init__(self, root: Path):
        self._matches: dict[str, list[Path]] = {}
        self._errors: dict[str, str] = {}
        try:
            entries = sorted(root.iterdir())
        except FileNotFoundError:
            return
        for entry in entries:
            named, _ = split_project_name(entry.name)
            if is_link(entry) or not entry.is_dir():
                if named:
                    self._errors.setdefault(named, "Project directory is not an owned folder")
                continue
            try:
                embedded = _project_marker_id(entry)
            except (OSError, ValueError):
                if named:
                    self._errors.setdefault(
                        named, "Project directory does not belong to this project"
                    )
                continue
            if embedded and named and embedded != named:
                for pid in (named, embedded):
                    self._errors.setdefault(
                        pid, "Project directory does not belong to this project"
                    )
                continue
            if pid := embedded or named:
                self._matches.setdefault(pid, []).append(entry)

    def find(self, pid: str) -> Path | None:
        if error := self._errors.get(pid):
            raise ValueError(error)
        matches = self._matches.get(pid, [])
        if len(matches) > 1:
            raise ValueError("Multiple directories claim the same project identity")
        return matches[0] if matches else None
