"""App-owned HTTP admission guard for native Desktop updates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.responses import JSONResponse

from .routers.credentials import desktop_only

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
SINGLE_SOURCE_SCHEMA_VERSION = 1
MULTI_SOURCE_SCHEMA_VERSION = 2
EXCLUDED_SUFFIX = ".lock"
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class RestoreOutcome:
    """What happened when rolling workspace data back to its pre-upgrade state."""

    status: str  # "restored", "absent", or "failed"
    backup: str | None = None
    files: int = 0
    detail: str | None = None


class _RestoreRejected(Exception):
    """The backup cannot be vouched for; restoring it would risk user data."""


def restore_latest_backup(workspace: str | Path) -> RestoreOutcome:
    """Restore the newest pre-upgrade snapshot written by the native updater.

    The shell snapshots the workspace before installing an update
    (``apps/desktop/src/migration/backup.rs``). When the upgraded engine then
    refuses the migrated data, this is the fallback that keeps user data on the
    version that wrote it instead of stranding it half-migrated. Every listed
    file is verified against the manifest (existence, size, SHA-256, no
    symlinks) before anything is copied, so a damaged backup is refused whole
    rather than partially restored. On success the manifest is renamed to
    ``MANIFEST.restored.json``: an interrupted restore leaves it in place and
    converges on the next launch, and an already-consumed backup never
    clobbers newer data. Retention of consumed backups is task 9.2.
    """
    root = Path(workspace).expanduser().resolve() / BACKUPS_DIR
    try:
        candidates = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return RestoreOutcome("absent")
    if not candidates:
        return RestoreOutcome("absent")
    # Only the newest backup is considered: falling back to an older snapshot
    # could roll data back past the upgrade the user actually attempted.
    newest = max(candidates, key=lambda entry: entry.name)
    if (newest / RESTORED_MARKER).is_file() or not (newest / MANIFEST_NAME).is_file():
        return RestoreOutcome("absent", str(newest))
    return _restore_from(newest)


def _restore_from(backup: Path) -> RestoreOutcome:
    try:
        manifest = json.loads((backup / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return RestoreOutcome("failed", str(backup), detail=f"unreadable manifest: {error}")
    try:
        plan = _restore_plan(manifest, backup)
    except _RestoreRejected as error:
        return RestoreOutcome("failed", str(backup), detail=str(error))
    try:
        for source, target in plan:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    except OSError as error:
        # The manifest survives until the last copy, so an interrupted restore
        # is retried in full by the next launch instead of staying partial.
        return RestoreOutcome("failed", str(backup), len(plan), f"copy failed: {error}")
    os.replace(backup / MANIFEST_NAME, backup / RESTORED_MARKER)
    return RestoreOutcome("restored", str(backup), len(plan))


def _restore_plan(manifest: object, backup: Path) -> list[tuple[Path, Path]]:
    if not isinstance(manifest, dict):
        raise _RestoreRejected("manifest is not an object")
    schema = manifest.get("schema_version")
    sources: list[tuple[str, Path]] = []
    if schema == MULTI_SOURCE_SCHEMA_VERSION:
        declared = manifest.get("sources")
        if not isinstance(declared, list) or not declared:
            raise _RestoreRejected("multi-source manifest declares no sources")
        for source in declared:
            if not isinstance(source, dict):
                raise _RestoreRejected("malformed backup source")
            name, path = source.get("name"), source.get("path")
            if not isinstance(name, str) or not name or not isinstance(path, str) or not path:
                raise _RestoreRejected("malformed backup source")
            sources.append((name, Path(path)))
    elif schema == SINGLE_SOURCE_SCHEMA_VERSION:
        source_path = manifest.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            raise _RestoreRejected("single-source manifest has no source path")
        sources.append(("", Path(source_path)))
    else:
        raise _RestoreRejected(f"unsupported manifest schema: {schema!r}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise _RestoreRejected("manifest lists no files")
    plan: list[tuple[Path, Path]] = []
    for entry in files:
        copy = _restore_entry(entry, sources, backup)
        if copy is not None:
            plan.append(copy)
    return plan


def _restore_entry(
    entry: object, sources: list[tuple[str, Path]], backup: Path
) -> tuple[Path, Path] | None:
    if not isinstance(entry, dict):
        raise _RestoreRejected("malformed manifest entry")
    relative, size, digest = entry.get("path"), entry.get("size"), entry.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not isinstance(digest, str)
    ):
        raise _RestoreRejected("malformed manifest entry")
    if relative in {MANIFEST_NAME, RESTORED_MARKER} or relative.endswith(EXCLUDED_SUFFIX):
        return None
    if relative.startswith("/") or "\\" in relative or ".." in PurePosixPath(relative).parts:
        raise _RestoreRejected(f"unsafe backup path: {relative}")
    source_root, rest = _locate_source(relative, sources)
    source_file = backup / relative
    target = source_root / rest if rest else source_root
    resolved_target = target.resolve()
    if resolved_target == backup or backup in resolved_target.parents:
        raise _RestoreRejected(f"backup source points at the backup itself: {relative}")
    if source_file.is_symlink() or not source_file.is_file():
        raise _RestoreRejected(f"missing backup file: {relative}")
    if source_file.stat().st_size != size:
        raise _RestoreRejected(f"size mismatch for {relative}")
    if _sha256(source_file) != digest.lower():
        raise _RestoreRejected(f"checksum mismatch for {relative}")
    return source_file, target


def _locate_source(relative: str, sources: list[tuple[str, Path]]) -> tuple[Path, str]:
    """Map a namespaced manifest path to its declared source root and remainder."""
    for name, root in sources:
        if not name:
            return root, relative
        if relative == name:
            return root, ""
        if relative.startswith(f"{name}/"):
            return root, relative[len(name) + 1 :]
    raise _RestoreRejected(f"backup file outside declared sources: {relative}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()
