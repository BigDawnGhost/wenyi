"""Desktop application assembly: one catalog, credential store, and runtime per app."""

from __future__ import annotations

import asyncio
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

from . import paths
from .babeldoc_plugin import BabeldocPlugin
from .diagnostics import DiagnosticsMiddleware, EngineDiagnostics, Reporter, scope
from .local_backend import PROJECT_SCHEMA_TOO_NEW, LocalBackend
from .local_credentials import CredentialStore, build_local_client, system_keyring
from .local_runtime import (
    LocalRuntime,
    progress_snapshot,
    run_workflow_sync,
    statistics_snapshot,
)
from .settings import DesktopSettings
from .updates import UpdateAdmissionMiddleware, UpdateGuard
from .updates import router as updates_router

# A local run can legitimately publish nothing for minutes (one long model call),
# and an idle socket looks exactly like a dead one to the renderer. The frame is
# the only proof that the link is still carrying data.
HEARTBEAT_SECONDS = 30.0
HEARTBEAT_FRAME = '{"kind": "heartbeat"}'


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

    def progress_snapshot(self, pid):
        return progress_snapshot(self.backend, self.runtime, pid)

    def statistics_snapshot(self, pid, job):
        return statistics_snapshot(self.runtime, pid, job)

    def health(self):
        with self.backend.transaction() as connection:
            connection.execute("SELECT 1")

    async def relay(self, ws, pid):
        with self.connect().subscribe(pid) as queue:
            project = await asyncio.to_thread(self.backend.get_project, pid)
            chapters = await asyncio.to_thread(self.backend.chapter_summaries, pid)
            await ws.send_json({"kind": "snapshot", "project": project or {}, "chapters": chapters})
            disconnected = asyncio.create_task(ws.receive())
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
                        await ws.send_text(HEARTBEAT_FRAME)
                        continue
                    data = received.result()
                    received = None
                    if data is None:
                        break
                    await ws.send_text(data)
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
    backend = LocalBackend(
        workspace,
        config_path,
        library_root=library_root,
        install_root=install_root,
    )
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
        read_storage_for=lambda pid: backend.storage_for(pid, create=False),
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
    )


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
    services = context.telemetry
    lifecycle = engine_diagnostics or EngineDiagnostics(diagnostics)

    @asynccontextmanager
    async def lifespan(app):
        with use_context(context), scope(diagnostics):
            with lifecycle.observe("runtime"):
                runtime = LocalRuntime(context.repository, context, diagnostics=diagnostics)
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
                        context.settings_store.credentials.clear_session()
                        context.repository.close()

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
        if context.settings_store.credentials.vault is None:
            diagnostics.emit("credentials_degraded")
    return app
