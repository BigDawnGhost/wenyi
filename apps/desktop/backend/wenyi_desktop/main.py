"""Desktop application assembly: one catalog, credential store, and runtime per app."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import Request, WebSocketDisconnect
from fastapi.responses import JSONResponse
from wenyi_backend.application import create_app as create_http_app
from wenyi_backend.context import BackendContext, use_context
from wenyi_backend.errors import ApiError, api_error_handler
from wenyi_core.errors import SchemaTooNew
from wenyi_core.observability import configure_logging

from . import catalog, paths
from .babeldoc_plugin import BabeldocPlugin
from .diagnostics import (
    DiagnosticsMiddleware,
    EngineDiagnostics,
    Reporter,
    StartupFailure,
    StartupFailureCode,
    scope,
)
from .local_backend import PROJECT_SCHEMA_TOO_NEW, LocalBackend
from .local_credentials import CredentialStore, build_local_client, system_keyring
from .local_runtime import (
    LocalRuntime,
    progress_snapshot,
    run_workflow_sync,
    statistics_snapshot,
)
from .settings import DesktopSettings
from .updates import (
    UpdateAdmissionMiddleware,
    UpdateGuard,
    UpgradeRecoveryRequired,
    check_upgrade_startup,
    upgrade_recovery_available,
)
from .updates import router as updates_router

log = logging.getLogger(__name__)

# A local run can legitimately publish nothing for minutes (one long model call),
# and an idle socket looks exactly like a dead one to the renderer. The frame is
# the only proof that the link is still carrying data.
HEARTBEAT_SECONDS = 30.0
HEARTBEAT_FRAME = '{"kind": "heartbeat"}'

# Old Desktop releases kept everything in one SQLite database and admitted any
# project folder beside it. The unified layout never adopts that data: the
# catalog refuses it and old folders stay unregistered. Startup only detects
# the situation so the user can be given the explicit steps below; nothing is
# migrated, moved, or rewritten (docs/development/desktop.md).
LEGACY_IMPORT_STEPS = (
    "Old-format Wenyi Desktop data was found in the selected data directory; "
    "this version does not migrate it automatically.",
    "A project folder that contains a current project.json (schema_version 2) "
    "can be brought over with the library's Import project action. Import moves "
    "the selected folder; import a copy if you need to keep the original.",
    "Folders without a current project.json use an unsupported old layout and "
    "cannot be imported; keep using the Wenyi version that created them or "
    "export their content from that version first.",
    "To start fresh, choose a new empty Desktop data directory; the old data is left as is.",
)
LEGACY_DATA_GUIDANCE = " ".join(LEGACY_IMPORT_STEPS)


def legacy_data_report(workspace: str | Path) -> dict:
    """Read-only scan of a data root for old-format Desktop data.

    The report never writes, adopts, or moves anything, and every failure
    degrades to "nothing found" so the check itself can never break startup.
    """
    root = Path(workspace)
    report: dict = {"legacy_catalog": False, "legacy_database": False, "old_project_folders": []}
    catalog_file = root / "catalog.sqlite3"
    if catalog_file.is_file():
        try:
            connection = sqlite3.connect(f"{catalog_file.resolve().as_uri()}?mode=ro", uri=True)
            try:
                populated = connection.execute(
                    "SELECT count(*) FROM sqlite_master"
                    " WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
                application_id = connection.execute("PRAGMA application_id").fetchone()[0]
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
            current = (catalog.APPLICATION_ID, catalog.FORMAT_VERSION)
            found = (application_id, user_version)
            report["legacy_catalog"] = bool(populated) and found != current
        except sqlite3.Error:
            # A catalog file that cannot be confirmed as current counts as old
            # for guidance only; catalog admission still decides startup.
            report["legacy_catalog"] = True
    projects = root / "projects"
    if projects.is_dir():
        try:
            entries = sorted(projects.iterdir())
        except OSError:
            # An unreadable projects directory still yields a usable report.
            entries = []
        for entry in entries:
            try:
                if not entry.is_dir() or entry.is_symlink():
                    continue
                if not any(entry.iterdir()):
                    continue
                if (entry / paths.PROJECT_DELETING).exists():
                    continue
                marker = entry / paths.PROJECT_MARKER
                if not marker.is_file():
                    report["old_project_folders"].append(entry.name)
                    continue
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report["old_project_folders"].append(entry.name)
                    continue
                schema = data.get("schema_version") if isinstance(data, dict) else None
                if schema != paths.PROJECT_SCHEMA_VERSION:
                    report["old_project_folders"].append(entry.name)
            except OSError:
                continue
    report["legacy_database"] = (root / "local.sqlite3").is_file()
    return report


def _report_legacy_data(workspace: str | Path) -> None:
    """Guide the user about old-format data on the sidecar's own stderr.

    stdout carries the parent/child protocol; a stray line before readiness
    would break the handshake, so guidance goes to stderr, which the shell
    only logs.
    """
    try:
        report = legacy_data_report(workspace)
    except OSError as error:
        # Guidance is best effort; never block startup on it.
        log.warning("Legacy data report failed: %s", error)
        return
    if report["legacy_catalog"] or report["legacy_database"] or report["old_project_folders"]:
        print(LEGACY_DATA_GUIDANCE, file=sys.stderr, flush=True)


class DesktopServices:
    def __init__(self, backend: LocalBackend):
        self.backend = backend
        self.runtime: LocalRuntime | None = None
        self.babeldoc = BabeldocPlugin(backend.workspace)

    def connect(self):
        if self.runtime is None:
            raise RuntimeError("Desktop workers have not started")
        return self.runtime.hub

    def release(self, cache):
        # The app owns the shared hub, individual jobs only borrow it.
        pass

    async def enqueue(self, name, **kwargs):
        if self.runtime is None:
            raise RuntimeError("Desktop workers have not started")
        return self.runtime.enqueue(name, **kwargs)

    def schedule_auto_resume(self, request, attempt: int, delay_seconds: int, next_retry_at: str):
        if self.runtime is not None:
            self.runtime.schedule_auto_resume(request, attempt, delay_seconds, next_retry_at)

    def cancel_auto_resume(self, pid: str) -> None:
        if self.runtime is not None:
            self.runtime.cancel_auto_resume(pid)

    def progress_snapshot(self, pid):
        return progress_snapshot(self.backend, self.runtime, pid)

    def statistics_snapshot(self, pid, job):
        return statistics_snapshot(self.runtime, pid, job)

    def health(self):
        with self.backend.transaction() as connection:
            connection.execute("SELECT 1")

    async def relay(self, websocket, pid):
        with self.connect().subscribe(pid) as queue:
            project = await asyncio.to_thread(self.backend.get_project, pid)
            chapters = await asyncio.to_thread(self.backend.chapter_summaries, pid)
            await websocket.send_json(
                {"kind": "snapshot", "project": project or {}, "chapters": chapters}
            )
            disconnected = asyncio.create_task(websocket.receive())
            received = None
            try:
                while not disconnected.done():
                    if received is None:
                        received = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {disconnected, received},
                        timeout=HEARTBEAT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if disconnected in done:
                        break
                    if received not in done:
                        # The wait stays pending, so a later event is still
                        # delivered on the next pass through the loop.
                        await websocket.send_text(HEARTBEAT_FRAME)
                        continue
                    data = received.result()
                    received = None
                    if data is None:
                        break
                    await websocket.send_text(data)
            except WebSocketDisconnect:
                pass
            finally:
                pending = [disconnected]
                if received is not None:
                    pending.append(received)
                for task in pending:
                    task.cancel()
                # An owning ASGI cancel scope may keep cancelling at each await.
                # Drain our children without turning their cancellation into a
                # second, unrelated CancelledError that escapes that scope.
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(*pending, return_exceptions=True)


def create_context(
    workspace: str | Path,
    *,
    api_token: str | None = None,
    config_path: str | None = None,
    credentials: CredentialStore | None = None,
    library_root: str | Path | None = None,
    install_root: str | Path | None = None,
) -> BackendContext:
    try:
        check_upgrade_startup(workspace)
    except UpgradeRecoveryRequired as error:
        raise StartupFailure(StartupFailureCode.UPGRADE_RECOVERY_REQUIRED) from error
    try:
        backend = LocalBackend(
            workspace,
            config_path,
            library_root=library_root,
            install_root=install_root,
        )
    except (RuntimeError, ValueError, sqlite3.Error, SchemaTooNew) as error:
        # An initialization error is not evidence that an upgrade failed.
        # Recovery is only ever initiated by a separate explicit offline command.
        if upgrade_recovery_available(workspace, os.environ.get("WENYI_DESKTOP_VERSION")):
            raise StartupFailure(StartupFailureCode.UPGRADE_RECOVERY_REQUIRED) from error
        if "not a current-format Desktop catalog" in str(error):
            raise StartupFailure(StartupFailureCode.LEGACY_CATALOG) from error
        raise
    _report_legacy_data(workspace)
    credentials = credentials or CredentialStore(backend.workspace, vault_factory=system_keyring)
    if credentials.database != backend.database:
        raise ValueError("Desktop credentials must belong to the selected data root")
    services = DesktopServices(backend)
    return BackendContext(
        repository=backend,
        settings_store=DesktopSettings(backend, credentials),
        exports=backend,
        telemetry=services,
        storage_for=backend.storage_for,
        read_storage_for=lambda pid: backend.storage_for(pid, create=False, upgrade=True),
        build_client=lambda config: build_local_client(config, credentials),
        mineru_token_resolver=credentials.resolve_mineru_token,
        connection_credential=credentials.resolve,
        babeldoc_connection=services.babeldoc.connection,
        run_workflow_sync=run_workflow_sync,
        enqueue=services.enqueue,
        data_dir=str(paths.projects(backend.workspace)),
        project_dir=lambda pid: str(backend.project_dir(pid)),
        new_project_dir=backend.new_project_dir,
        source_paths=backend,
        health=services.health,
        update_term=lambda pid, storage, source, term: storage.update_term(source, term),
        api_token=api_token,
        remove_project=backend.remove_project,
        schedule_auto_resume=services.schedule_auto_resume,
        cancel_auto_resume=services.cancel_auto_resume,
    )


def verify_version_handshake() -> None:
    """Refuse to serve when the native shell and this engine are different builds.

    The shell injects the expected PEP 440 distribution version from the same
    build identity as its native SemVer. Compare the complete installed version,
    including prerelease and local identifiers. Older launchers and uninstalled
    source builds can lack one side of the check; keep their warning-only path.
    """
    expected_version = os.environ.get("WENYI_DESKTOP_ENGINE_VERSION")
    if expected_version is None:
        # Older launchers supplied only the native version (identical for stable releases).
        expected_version = os.environ.get("WENYI_DESKTOP_VERSION")
    if expected_version is None:
        print(
            "wenyi-desktop: launcher did not set WENYI_DESKTOP_VERSION; "
            "skipping the shell/engine version handshake.",
            file=sys.stderr,
            flush=True,
        )
        return
    try:
        engine_version = importlib.metadata.version("wenyi-desktop")
    except importlib.metadata.PackageNotFoundError:
        print(
            "wenyi-desktop: installed engine version is unknown; "
            "skipping the shell/engine version handshake.",
            file=sys.stderr,
            flush=True,
        )
        return
    if engine_version != expected_version:
        raise StartupFailure(StartupFailureCode.VERSION_MISMATCH)


def create_app(
    workspace: str | Path | None = None,
    *,
    context: BackendContext | None = None,
    api_token: str | None = None,
    diagnostics: Reporter | None = None,
    library_root: str | Path | None = None,
    install_root: str | Path | None = None,
    engine_diagnostics: EngineDiagnostics | None = None,
):
    verify_version_handshake()

    from .desktop import ORIGINS
    from .routers import (
        babeldoc_plugin,
        credentials,
        desktop_export,
        external_credentials,
        library_location,
        project_import,
    )

    # The sidecar owns its own stderr; stdout carries the parent/child protocol.
    configure_logging(service="wenyi-desktop")

    if context is None:
        if workspace is None:
            raise ValueError("Desktop requires an explicit workspace")
        context = create_context(
            workspace,
            api_token=api_token,
            library_root=library_root,
            install_root=install_root,
        )
    backend = context.repository
    assert isinstance(backend, LocalBackend)
    settings = context.settings_store
    assert isinstance(settings, DesktopSettings)
    services = context.telemetry
    assert isinstance(services, DesktopServices)
    lifecycle = engine_diagnostics or EngineDiagnostics(diagnostics)

    @asynccontextmanager
    async def lifespan(app):
        with use_context(context), scope(diagnostics):
            with lifecycle.observe("runtime"):
                runtime = LocalRuntime(backend, context, diagnostics=diagnostics)
            services.runtime = runtime
            try:
                with lifecycle.observe("runtime"):
                    await runtime.start()
                with lifecycle.observe("plugins"):
                    services.babeldoc.restore()
                yield
            finally:
                with lifecycle.observe("shutdown"):
                    try:
                        await runtime.stop()
                    finally:
                        await services.babeldoc.stop()
                        services.runtime = None
                        settings.credentials.clear_session()
                        backend.close()

    app = create_http_app(
        context,
        lifespan=lifespan,
        origins=list(ORIGINS),
        handle_errors=diagnostics is None,
    )

    @app.exception_handler(SchemaTooNew)
    async def schema_too_new(request: Request, error: SchemaTooNew) -> JSONResponse:
        # Read stores open lazily, after storage_for has returned. Keep their
        # refusal identical to an eager writable open without changing that lifetime.
        return await api_error_handler(
            request, ApiError(PROJECT_SCHEMA_TOO_NEW, 409, error.message)
        )

    app.include_router(credentials.router)
    app.include_router(desktop_export.router)
    app.include_router(external_credentials.router)
    app.include_router(updates_router)
    app.include_router(babeldoc_plugin.router)
    app.include_router(project_import.router)
    app.include_router(library_location.router)
    app.state.babeldoc_plugin = services.babeldoc
    app.state.update_guard = UpdateGuard(services)
    app.add_middleware(
        UpdateAdmissionMiddleware, guard=app.state.update_guard, token=context.api_token
    )
    if diagnostics is not None:
        app.add_middleware(DiagnosticsMiddleware, reporter=diagnostics, origins=ORIGINS)
        if settings.credentials.vault is None:
            diagnostics.emit("credentials_degraded")
    return app
