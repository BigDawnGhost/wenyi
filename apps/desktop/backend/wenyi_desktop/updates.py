"""App-owned HTTP admission guard for native Desktop updates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.responses import JSONResponse
from wenyi_core.storage.io_path import plain_path, to_io_path
from wenyi_core.storage.locks import FileLockHandle, StorageBusyError

from . import paths
from .routers.credentials import desktop_only

log = logging.getLogger(__name__)

CONTROL_PATHS = frozenset(
    {
        "/desktop/updates/prepare",
        "/desktop/updates/cancel",
        "/desktop/updates/backup-plan",
        "/desktop/plugins/babeldoc/disable",
        "/desktop/plugins/babeldoc/update",
        "/desktop/plugins/babeldoc/cancel-update",
    }
)


class UpdateGuard:
    """Serialize control requests; admission changes never cross an await."""

    def __init__(self, services):
        self.services = services
        self.closed = False
        self.inflight = 0
        self._control = asyncio.Lock()
        self._plugin_control = False

    def runtime(self):
        runtime = self.services.runtime
        if runtime is None or not runtime.accepting:
            raise HTTPException(503, "Desktop workers are unavailable")
        return runtime

    async def prepare(self, *, allow_plugin_activity: bool = False) -> None:
        async with self._control:
            runtime = self.runtime()
            if self.closed:
                if not allow_plugin_activity and not self._plugin_control:
                    return
                raise HTTPException(409, "Desktop admission is already closed")
            self.closed = True
            self._plugin_control = allow_plugin_activity
            committed = False
            try:
                plugin = getattr(self.services, "babeldoc", None)
                if not allow_plugin_activity and plugin is not None and plugin.busy:
                    raise HTTPException(409, "A plugin operation is in progress")
                if self.inflight:
                    raise HTTPException(409, "Desktop mutations are in progress")
                jobs = await asyncio.to_thread(self.services.backend.all_jobs)
                if any(job["status"] in {"queued", "running"} for job in jobs):
                    raise HTTPException(409, "Desktop jobs are in progress")
                if self.runtime() is not runtime or not runtime.idle:
                    raise HTTPException(409, "Desktop tasks are in progress")
                committed = True
            finally:
                # Includes CancelledError: a tentative guard must never survive failure.
                if not committed:
                    self.closed = False

    async def cancel(self, *, allow_plugin_activity: bool = False) -> None:
        # A cancel arriving during the check waits for that check to finish. It
        # cannot reopen admission while another prepare is inspecting the catalog.
        async with self._control:
            if not allow_plugin_activity:
                self.runtime()
            if self.closed and self._plugin_control != allow_plugin_activity:
                raise HTTPException(409, "Another operation owns Desktop admission")
            self.closed = False

    async def backup_plan(self) -> dict:
        """Resolve owned data while the native exclusive lease and gate are held."""
        async with self._control:
            self.runtime()
            if not self.closed or self._plugin_control:
                raise HTTPException(409, "Prepare the Desktop update before requesting a backup")
            return await asyncio.to_thread(self._backup_plan)

    def _backup_plan(self) -> dict:
        backend = self.services.backend
        if not backend.database.is_file():
            raise HTTPException(409, "The Desktop catalog is unavailable; update was cancelled")
        # The native snapshot adds WAL/SHM after stopping the engine: clean
        # shutdown may remove them, and forced shutdown may leave new ones.
        # Never copy the root, which also holds the backup destination.
        sources = [{"name": "catalog.sqlite3", "path": str(backend.database)}]
        for index, directory in enumerate(backend.project_directories()):
            sources.append({"name": f"projects/{index}", "path": str(directory)})
        return {"schema_version": 1, "sources": sources}


class UpdateAdmissionMiddleware:
    """Count the entire ASGI mutation, including streamed uploads and responses."""

    def __init__(self, app, *, guard: UpdateGuard, token: str | None):
        self.app = app
        self.guard = guard
        self.token = token

    async def __call__(self, scope, receive, send):
        mutation = scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        control = mutation and scope["method"] == "POST" and scope["path"] in CONTROL_PATHS
        if not mutation or control:
            return await self.app(scope, receive, send)
        # Preserve authentication precedence even when the admission gate is closed.
        headers = dict(scope["headers"])
        provided = headers.get(b"authorization", b"").removeprefix(b"Bearer ").strip()
        if self.token and not secrets.compare_digest(provided, self.token.encode("utf-8")):
            response = JSONResponse({"detail": "invalid api token"}, status_code=401)
            return await response(scope, receive, send)
        if self.guard.closed:
            response = JSONResponse({"detail": "Desktop is preparing an update"}, status_code=409)
            return await response(scope, receive, send)
        self.guard.inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.guard.inflight -= 1


router = APIRouter(
    prefix="/desktop/updates",
    dependencies=[Depends(desktop_only)],
    include_in_schema=False,
)


@router.post("/prepare", status_code=204)
async def prepare(request: Request) -> Response:
    await request.app.state.update_guard.prepare()
    return Response(status_code=204)


@router.post("/cancel", status_code=204)
async def cancel(request: Request) -> Response:
    await request.app.state.update_guard.cancel()
    return Response(status_code=204)


@router.post("/backup-plan")
async def backup_plan(request: Request) -> dict:
    return await request.app.state.update_guard.backup_plan()


BACKUPS_DIR = "backups"
MANIFEST_NAME = "MANIFEST.json"
RESTORED_MARKER = "MANIFEST.restored.json"
MULTI_SOURCE_SCHEMA_VERSION = 2
ATTEMPT_NAME = ".upgrade-attempt.json"
RECOVERY_DIR = "upgrade-recovery"
_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")
CATALOG_FILES = tuple(f"catalog.sqlite3{suffix}" for suffix in _SQLITE_SUFFIXES)
_HASH_CHUNK_BYTES = 1024 * 1024
RETENTION_KEEP_BACKUPS = 3
RETENTION_MAX_BYTES = 5 * 1024**3
TRASH_DIR = "backups-trash"
_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.+-]{0,198}[A-Za-z0-9])?")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROJECT = re.compile(r"projects/(0|[1-9][0-9]*)")


@dataclass(frozen=True)
class RestoreOutcome:
    """What happened when rolling workspace data back to its pre-upgrade state."""

    status: str  # "available", "restored", "absent", or "failed"
    backup: str | None = None
    files: int = 0
    detail: str | None = None


class _RestoreRejected(Exception):
    """The backup cannot be vouched for; restoring it would risk user data."""


def _root(workspace: str | Path) -> Path:
    try:
        return Path(plain_path(Path(workspace).expanduser().resolve()))
    except RuntimeError as error:
        raise _RestoreRejected("Could not resolve the Desktop data root") from error


def _owned(path: Path, *, directory: bool, missing: bool = False) -> os.stat_result | None:
    """Refuse links, special files, and (on POSIX) entries owned by another user."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    right_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not right_type
        or getattr(info, "st_file_attributes", 0) & 0x400
        or (not directory and info.st_nlink != 1)
        or (os.name == "posix" and info.st_uid != os.geteuid())
    ):
        raise _RestoreRejected("An upgrade recovery path is not an owned regular file or directory")
    return info


def _json_object(raw: bytes) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise _RestoreRejected("Upgrade recovery metadata is not an object")
    return value


def _attempt(root: Path) -> dict | None:
    backups = root / BACKUPS_DIR
    if _owned(backups, directory=True, missing=True) is None:
        return None
    marker = backups / ATTEMPT_NAME
    if _owned(marker, directory=False, missing=True) is None:
        return None
    value = _json_object(marker.read_bytes())
    if (
        value.get("schema_version") != 1
        or not _ID.fullmatch(str(value.get("snapshot", "")))
        or not _ID.fullmatch(str(value.get("target_version", "")))
        or not _DIGEST.fullmatch(str(value.get("manifest_sha256", "")))
        or value.get("phase") not in ("prepared", "quarantining", "publishing")
        or not all(
            isinstance(value[key], str) for key in ("snapshot", "target_version", "manifest_sha256")
        )
    ):
        raise _RestoreRejected("The upgrade attempt marker is invalid")
    return value


def check_upgrade_startup(workspace: str | Path) -> None:
    """Check before SQLite can see a partially published restore."""
    try:
        attempt = _attempt(_root(workspace))
        if attempt is not None and attempt["phase"] != "prepared":
            raise _RestoreRejected("Resume the interrupted upgrade recovery before opening Wenyi")
    except (OSError, ValueError, _RestoreRejected) as error:
        raise UpgradeRecoveryRequired from error


def upgrade_recovery_available(workspace: str | Path, version: str | None) -> bool:
    """Describe eligibility, not the cause of a startup failure."""
    if version is None:
        return False
    try:
        root = _root(workspace)
        attempt = _attempt(root)
        if attempt is None or attempt["target_version"] != version:
            return False
        backup = root / BACKUPS_DIR / attempt["snapshot"]
        marker = backup / MANIFEST_NAME
        _owned(backup, directory=True)
        _owned(marker, directory=False)
        return _sha256(marker) == attempt["manifest_sha256"]
    except (OSError, ValueError, _RestoreRejected):
        return False


class UpgradeRecoveryRequired(RuntimeError):
    """The workspace cannot safely be opened until the recovery marker is resolved."""


def finish_upgrade_startup(workspace: Path) -> bool:
    """Consume an armed attempt after a healthy start, before accepting user writes."""
    try:
        workspace = _root(workspace)
        check_upgrade_startup(workspace)
        if _attempt(workspace) is None:
            return False
        _clear_attempt(workspace)
        return True
    except (OSError, ValueError, _RestoreRejected) as error:
        raise UpgradeRecoveryRequired from error


def _sync_directory(directory: Path) -> None:
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _move(source: Path, target: Path, *, replace: bool = False) -> None:
    if not replace and (target.exists() or paths.is_link(target)):
        raise _RestoreRejected("An upgrade recovery destination is occupied")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        # WRITE_THROUGH; never permit a cross-volume copy in place of a rename.
        if not move(to_io_path(source), to_io_path(target), 0x8 | int(replace)):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target) if replace else os.rename(source, target)
    _sync_directory(target.parent)
    if target.parent != source.parent:
        _sync_directory(source.parent)


def _clear_attempt(root: Path) -> None:
    marker = root / BACKUPS_DIR / ATTEMPT_NAME
    retired = marker.with_name(".retired-upgrade-attempt.json")
    _owned(retired, directory=False, missing=True)
    _move(marker, retired, replace=True)


def _phase(root: Path, attempt: dict, phase: str) -> None:
    directory = root / BACKUPS_DIR
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".attempt-", delete=False) as temporary:
        file = Path(temporary.name)
        try:
            temporary.write(json.dumps({**attempt, "phase": phase}).encode("utf-8"))
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary.close()
            file.unlink()
            raise
    try:
        _move(file, directory / ATTEMPT_NAME, replace=True)
    finally:
        file.unlink(missing_ok=True)
    attempt["phase"] = phase


def _safe_parts(value: str) -> list[str]:
    parts = value.split("/")
    if any(
        part in {"", ".", ".."}
        or "\x00" in part
        or "\\" in part
        or (
            os.name == "nt"
            and (
                part.endswith((".", " "))
                or any(ord(char) < 32 or char in ':<>|?*"' for char in part)
                or PureWindowsPath(part).is_reserved()
            )
        )
        for part in parts
    ):
        raise _RestoreRejected("Unsafe path in upgrade snapshot")
    return parts


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    sources: dict[str, Path]
    files: dict[str, tuple[int, str]]
    created_at: datetime
    size: int


def _tree(root: Path, *, device: int | None = None) -> Iterator[tuple[Path, os.stat_result]]:
    """Walk only owned, unlinked entries; never descend into another volume."""
    for entry in root.iterdir():
        attrs = entry.lstat()
        directory = stat.S_ISDIR(attrs.st_mode)
        verified = _owned(entry, directory=directory)
        assert verified is not None
        if device is not None and (attrs.st_dev != device or (directory and entry.is_mount())):
            raise _RestoreRejected("Upgrade recovery will not change a mounted directory")
        if directory:
            yield from _tree(entry, device=device)
        else:
            yield entry, verified


def _snapshot(backup: Path, *, expected: str | None = None) -> _Snapshot:
    _owned(backup, directory=True)
    marker = backup / MANIFEST_NAME
    if expected is None and _owned(marker, directory=False, missing=True) is None:
        marker = backup / RESTORED_MARKER  # Valid historical snapshots are not restore candidates.
    _owned(marker, directory=False)
    raw = marker.read_bytes()
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        raise _RestoreRejected("The snapshot manifest does not match this upgrade attempt")
    manifest = _json_object(raw)
    if manifest.get("schema_version") != MULTI_SOURCE_SCHEMA_VERSION:
        raise _RestoreRejected("Unsupported upgrade snapshot format")
    # Chrono emits up to nanoseconds; Python 3.10 accepts microseconds.
    timestamp = re.sub(r"(\.\d{6})\d+", r"\1", str(manifest.get("created_at")))
    created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if created_at.utcoffset() is None:
        raise _RestoreRejected("Upgrade snapshot is missing its creation time zone")
    sources: dict[str, Path] = {}
    declared, entries = manifest.get("sources"), manifest.get("files")
    if not isinstance(declared, list) or not isinstance(entries, list):
        raise _RestoreRejected("Upgrade snapshot is missing its source inventory")
    for source in declared:
        if not isinstance(source, dict):
            raise _RestoreRejected("Invalid upgrade snapshot source")
        name, path_value = source.get("name"), source.get("path")
        path = Path(plain_path(path_value)) if isinstance(path_value, str) else Path()
        if (
            not isinstance(name, str)
            or not path.is_absolute()
            or (name not in CATALOG_FILES and not _PROJECT.fullmatch(name))
            or name in sources
            or path in sources.values()
        ):
            raise _RestoreRejected("Invalid or duplicate upgrade snapshot source")
        sources[name] = path
    files: dict[str, tuple[int, str]] = {}
    folded: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise _RestoreRejected("Invalid upgrade snapshot file")
        relative, size, digest = entry.get("path"), entry.get("size"), entry.get("sha256")
        if (
            not isinstance(relative, str)
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise _RestoreRejected("Invalid or duplicate upgrade snapshot file")
        spelling = relative.casefold() if os.name == "nt" else relative
        if spelling in folded:
            raise _RestoreRejected("Invalid or duplicate upgrade snapshot file")
        parts = _safe_parts(relative)
        namespace = "/".join(parts[:2]) if len(parts) > 2 else relative
        if (
            namespace not in sources
            or (len(parts) <= 2 and relative not in CATALOG_FILES)
            or parts[-1].lower().endswith(".lock")
        ):
            raise _RestoreRejected("Upgrade snapshot file is outside its declared sources")
        files[relative] = (size, digest)
        folded.add(spelling)
    if any((name in sources) != (name in files) for name in CATALOG_FILES):
        raise _RestoreRejected("An upgrade snapshot catalog file is missing")
    if CATALOG_FILES[0] not in files:
        raise _RestoreRejected("Upgrade snapshot has no catalog")
    remaining = dict(files)
    for path, data in _tree(backup):
        if path == marker:
            continue
        relative = path.relative_to(backup).as_posix()
        if relative not in remaining:
            raise _RestoreRejected("Unexpected file in upgrade snapshot")
        size, digest = remaining.pop(relative)
        if data.st_size != size or _sha256(path) != digest:
            raise _RestoreRejected("Upgrade snapshot failed file verification")
    if remaining:
        raise _RestoreRejected("A file is missing from the upgrade snapshot")
    with (backup / CATALOG_FILES[0]).open("rb") as catalog:
        if catalog.read(16) != b"SQLite format 3\x00":
            raise _RestoreRejected("Upgrade snapshot catalog is not a SQLite database")
    return _Snapshot(
        backup, sources, files, created_at, len(raw) + sum(s for s, _ in files.values())
    )


def _project_pid(directory: Path) -> str:
    marker = directory / paths.PROJECT_MARKER
    _owned(marker, directory=False)
    data = _json_object(marker.read_bytes())
    pid = data.get("pid")
    if (
        not isinstance(pid, str)
        or not paths.project_id(pid)
        or data.get("schema_version") != paths.PROJECT_SCHEMA_VERSION
        or paths.deleting_marker_in(directory).exists()
        or paths.is_link(paths.deleting_marker_in(directory))
    ):
        raise _RestoreRejected("Upgrade recovery cannot verify a project identity")
    return pid


def _restore_units(root: Path, snapshot: _Snapshot) -> dict[str, Path]:
    projects = paths.projects(root)
    units = {name: root / name for name in CATALOG_FILES}
    pids = set()
    for name, target in snapshot.sources.items():
        if name in CATALOG_FILES:
            if target != units[name]:
                raise _RestoreRejected("Upgrade snapshot belongs to a different data root")
            continue
        if target.parent != projects or target.name in {".", ".."}:
            raise _RestoreRejected("Recovery refuses projects in external libraries")
        _safe_parts(target.name)
        pid = _project_pid(snapshot.path / name)
        named, _ = paths.split_project_name(target.name)
        if (named and named != pid) or pid in pids:
            raise _RestoreRejected("Upgrade snapshot has conflicting project identities")
        units[name] = target
        pids.add(pid)
    return units


@contextmanager
def _offline(root: Path) -> Iterator[None]:
    """Exclude native, standalone and surviving engine owners before any recovery work."""
    _owned(root, directory=True)
    locks = paths.locks(root)
    locks.mkdir(mode=0o700, exist_ok=True)
    _owned(locks, directory=True)
    with ExitStack() as stack:

        def acquire(path: Path) -> None:
            _owned(path, directory=False, missing=True)
            handle = FileLockHandle(path)
            stack.callback(handle.release)
            handle.acquire(blocking=False)

        acquire(locks / ".desktop-profile-admission.lock")
        acquire(paths.native_instance_lock(root))
        acquire(locks / ".desktop-profile.lock")
        acquire(paths.instance_lock(root))
        writers = locks / ".desktop-writers"
        if _owned(writers, directory=True, missing=True) is not None:
            for lease in writers.iterdir():
                acquire(lease)
        yield


def _check_staged_databases(staged: Path, snapshot: _Snapshot) -> None:
    databases = [
        CATALOG_FILES[0],
        *(f"{name}/state.sqlite3" for name in snapshot.sources if _PROJECT.fullmatch(name)),
    ]
    for database in databases:
        if database not in snapshot.files:
            continue  # A registered project may not have initialized state yet.
        with tempfile.TemporaryDirectory(prefix=".sqlite-", dir=staged.parent) as temporary:
            copy = Path(temporary) / "database.sqlite3"
            for suffix in _SQLITE_SUFFIXES:
                relative = database + suffix
                if relative in snapshot.files:
                    shutil.copyfile(staged / relative, f"{copy}{suffix}")
            # Allow WAL replay and hot-journal rollback on this disposable
            # copy. Opening staged data itself could change the verified bytes.
            try:
                with closing(sqlite3.connect(f"{copy.as_uri()}?mode=rw", uri=True)) as connection:
                    connection.execute("PRAGMA trusted_schema=OFF")
                    result = connection.execute("PRAGMA quick_check").fetchall()
            except sqlite3.Error as error:
                raise _RestoreRejected("Upgrade snapshot failed SQLite integrity check") from error
            if result != [("ok",)]:
                raise _RestoreRejected("Upgrade snapshot failed SQLite integrity check")


def _prepare_restore(
    root: Path, transaction: Path, snapshot: _Snapshot, units: dict[str, Path]
) -> None:
    # Validate every target before the first rename. Project directories move
    # whole, including unknown later files, so no unmatched SQLite sidecar survives.
    device = root.stat().st_dev
    for name, target in units.items():
        directory = name not in CATALOG_FILES
        info = _owned(target, directory=directory, missing=True)
        if info is None:
            continue
        if info.st_dev != device or (directory and target.is_mount()):
            raise _RestoreRejected("Upgrade recovery will not change a mounted directory")
        if directory:
            if _project_pid(target) != _project_pid(snapshot.path / name):
                raise _RestoreRejected("Live and backed up project identities do not match")
            for _ in _tree(target, device=device):
                pass
    staged = transaction / "staged"
    if _owned(staged, directory=True, missing=True) is not None:
        return  # Its atomic publication is the durable verification checkpoint.
    with tempfile.TemporaryDirectory(prefix=".build-", dir=transaction) as temporary:
        building = Path(temporary) / "staged"
        building.mkdir()
        for relative, (size, digest) in snapshot.files.items():
            # A complete snapshot is verified again as files are staged.
            target = building / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(mode=0o600, exist_ok=False)
            shutil.copyfile(snapshot.path / relative, target)
            with target.open("rb+") as file:
                os.fsync(file.fileno())
            if target.stat().st_size != size or _sha256(target) != digest:
                raise _RestoreRejected("Staged upgrade snapshot failed file verification")
        _check_staged_databases(building, snapshot)
        for directory, _, _ in os.walk(building, topdown=False):
            _sync_directory(Path(directory))
        _move(building, staged)


def _recover(root: Path, attempt: dict) -> RestoreOutcome:
    name = attempt["snapshot"]
    snapshot = _snapshot(root / BACKUPS_DIR / name, expected=attempt["manifest_sha256"])
    units = _restore_units(root, snapshot)
    parent = root / RECOVERY_DIR
    transaction = parent / name
    quarantine = transaction / "preserved"
    # Every mutation is confined to the canonical selected root. External
    # projects are deliberately not split off into a half-restored catalog.
    directories = (parent, transaction, quarantine, paths.projects(root), quarantine / "projects")
    for directory in directories:
        directory.mkdir(mode=0o700, exist_ok=True)
        _owned(directory, directory=True)
        if directory.stat().st_dev != root.stat().st_dev or directory.is_mount():
            raise _RestoreRejected("Upgrade recovery requires one local volume")
        _sync_directory(directory.parent)
    staged = transaction / "staged"
    if attempt["phase"] == "prepared":
        _prepare_restore(root, transaction, snapshot, units)
        _phase(root, attempt, "quarantining")
    _owned(staged, directory=True)
    if any(name not in CATALOG_FILES for name in units):
        _owned(staged / "projects", directory=True)
    # Recheck staged path ownership on resume, before moving any remaining data.
    for _ in _tree(staged, device=root.stat().st_dev):
        pass
    if attempt["phase"] == "quarantining":
        for key, live in units.items():
            if _owned(live, directory=key not in CATALOG_FILES, missing=True) is not None:
                _move(live, quarantine / key)
        _phase(root, attempt, "publishing")
    for key, live in units.items():
        directory = key not in CATALOG_FILES
        replacement = staged / key
        exists = _owned(replacement, directory=directory, missing=True) is not None
        installed = _owned(live, directory=directory, missing=True) is not None
        if exists:
            _move(replacement, live)
        elif installed != (key in snapshot.sources):
            raise _RestoreRejected("An expected upgrade recovery file is missing or occupied")
    _verify_publication(snapshot, units)
    _clear_attempt(root)
    return RestoreOutcome("restored", name, len(snapshot.files))


def _verify_publication(snapshot: _Snapshot, units: dict[str, Path]) -> None:
    remaining = dict(snapshot.files)
    for name in snapshot.sources:
        live = units[name]
        if name in CATALOG_FILES:
            info = _owned(live, directory=False)
            assert info is not None
            files = [(live, info)]
        else:
            files = _tree(live)
        for path, info in files:
            key = name if name in CATALOG_FILES else f"{name}/{path.relative_to(live).as_posix()}"
            expected = remaining.pop(key, None)
            if expected is None or (info.st_size, _sha256(path)) != expected:
                raise _RestoreRejected("Published upgrade data did not match its snapshot")
    if remaining:
        raise _RestoreRejected("Published upgrade data is incomplete")


def pending_upgrade(workspace: str | Path, version: str) -> RestoreOutcome:
    """Let the offline command report the exact snapshot ID without starting recovery."""
    try:
        attempt = _attempt(_root(workspace))
        if attempt is None:
            return RestoreOutcome("absent", detail="No unfinished Desktop upgrade")
        if attempt["target_version"] != version:
            raise _RestoreRejected("Use the Wenyi version that this in-app update installed")
        return RestoreOutcome("available", attempt["snapshot"])
    except _RestoreRejected as error:
        return RestoreOutcome("failed", detail=str(error))
    except (OSError, ValueError):
        return RestoreOutcome("failed", detail="The Desktop upgrade marker could not be read")


def recover_upgrade(workspace: str | Path, snapshot_id: str, version: str) -> RestoreOutcome:
    """Explicit offline recovery; ordinary startup never runs this transaction."""
    try:
        root = _root(workspace)
        with _offline(root):
            attempt = _attempt(root)
            if attempt is None or attempt["snapshot"] != snapshot_id:
                return RestoreOutcome("absent", detail="No matching unfinished Desktop upgrade")
            if attempt["target_version"] != version:
                raise _RestoreRejected("Use the Wenyi version that this in-app update installed")
            return _recover(root, attempt)
    except StorageBusyError:
        return RestoreOutcome("failed", detail="Close every Wenyi process before recovery")
    except _RestoreRejected as error:
        return RestoreOutcome("failed", detail=str(error))
    except (OSError, ValueError):
        return RestoreOutcome("failed", detail="Upgrade recovery could not verify or move its data")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PruneOutcome:
    """Which upgrade snapshots survived retention and which were cleared."""

    kept: tuple[str, ...] = ()
    evicted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def prune_backups(
    workspace: str | Path,
    *,
    keep: int = RETENTION_KEEP_BACKUPS,
    max_bytes: int = RETENTION_MAX_BYTES,
    delete: Callable[[Path], None] | None = None,
) -> PruneOutcome:
    """Retain actual verified snapshots, protecting an active or performed recovery."""
    try:
        root = _root(workspace)
        directory = root / BACKUPS_DIR
        if _owned(directory, directory=True, missing=True) is None:
            return PruneOutcome()
        candidates = sorted(directory.iterdir())
        attempt = _attempt(root)
        protected = {attempt["snapshot"]} if attempt is not None else set()
        recovery = root / RECOVERY_DIR
        if _owned(recovery, directory=True, missing=True) is not None:
            # A recovery directory preserves the pre-recovery live data. Never
            # evict the snapshot paired with it, even beyond the normal budget.
            protected.update(entry.name for entry in recovery.iterdir())
    except (OSError, ValueError, _RestoreRejected):
        log.warning("Backups directory scan failed")
        return PruneOutcome()

    snapshots = []
    for candidate in candidates:
        if not _ID.fullmatch(candidate.name):
            continue
        try:
            snap = _snapshot(candidate)
            snapshots.append(snap)
            if (candidate / RESTORED_MARKER).is_file():
                protected.add(candidate.name)
        except (OSError, ValueError, _RestoreRejected):
            # Incomplete, corrupted or foreign directories stay untouched.
            continue
    snapshots.sort(key=lambda snap: snap.created_at, reverse=True)
    if snapshots:
        protected.add(snapshots[0].path.name)

    kept: list[Path] = []
    evicted: list[Path] = []
    failed: list[Path] = []
    count = sum(snap.path.name in protected for snap in snapshots)
    budget = sum(snap.size for snap in snapshots if snap.path.name in protected)
    for snap in snapshots:
        entry = snap.path
        if entry.name in protected:
            kept.append(entry)
            continue
        if count < keep and budget + snap.size <= max_bytes:
            kept.append(entry)
            count += 1
            budget += snap.size
            continue
        try:
            if delete is not None:
                delete(entry)
            else:
                _recoverable_delete(entry, root / TRASH_DIR)
        except (OSError, _RestoreRejected):
            failed.append(entry)
        else:
            evicted.append(entry)
    return PruneOutcome(
        kept=tuple(entry.name for entry in kept),
        evicted=tuple(entry.name for entry in evicted),
        failed=tuple(entry.name for entry in failed),
    )


def _recoverable_delete(path: Path, trash_root: Path) -> None:
    """Delete through a recoverable path: the OS recycle bin, else a trash move.

    A retention mistake must never destroy the only copy of a snapshot. On
    Windows the directory goes to the recycle bin (FOF_ALLOWUNDO), where the
    user can restore it; anywhere else, or when that fails, it is moved to
    'backups-trash' beside the backups it came from. Permanent deletion is
    never attempted: if both recoverable paths fail, the backup stays in place
    and the caller reports it as failed.
    """
    if _recycle_delete(path):
        return
    trash_root.mkdir(parents=True, exist_ok=True)
    _owned(trash_root, directory=True)
    target = trash_root / path.name
    if target.exists() or paths.is_link(target):
        target = trash_root / f"{path.name}-{time.time_ns()}"
    _move(path, target)


def _recycle_delete(path: Path) -> bool:
    """Move path into the Windows recycle bin; False when unavailable or refused."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", ctypes.c_uint),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        # FO_DELETE with FOF_ALLOWUNDO | FOF_SILENT | FOF_NOCONFIRMATION: the
        # directory becomes restorable from the recycle bin, without dialogs.
        operation = SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=3,
            pFrom=str(path) + "\0",
            pTo=None,
            fFlags=0x40 | 0x4 | 0x10,
        )
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except Exception:
        # The recycle-bin API fails in arbitrary win32 ways; every failure
        # just falls back to the trash move in the caller.
        return False
    return code == 0 and not operation.fAnyOperationsAborted
