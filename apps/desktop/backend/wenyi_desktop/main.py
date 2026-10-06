"""Desktop application assembly: one catalog, credential store, and runtime per app."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import WebSocketDisconnect
from wenyi_backend import dal
from wenyi_backend.application import create_app as create_http_app
from wenyi_backend.context import BackendContext, use_context

from .local_backend import LocalBackend
from .local_credentials import CredentialStore, build_local_client, system_keyring
from .local_runtime import LocalRuntime, progress_snapshot, statistics_snapshot
from .settings import DesktopSettings
from .updates import UpdateAdmissionMiddleware, UpdateGuard
from .updates import router as updates_router


class DesktopServices:
    def __init__(self, backend: LocalBackend):
        self.backend = backend
        self.runtime: LocalRuntime | None = None

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
            project = await asyncio.to_thread(dal.get_project, pid)
            chapters = await asyncio.to_thread(dal.chapter_summaries, pid)
            await ws.send_json({"kind": "snapshot", "project": project or {}, "chapters": chapters})
            disconnected = asyncio.create_task(ws.receive())
            received = None
            try:
                while not disconnected.done():
                    received = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {disconnected, received}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if disconnected in done:
                        break
                    data = received.result()
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
) -> BackendContext:
    backend = LocalBackend(workspace, config_path)
    credentials = credentials or CredentialStore(backend.workspace, vault_factory=system_keyring)
    services = DesktopServices(backend)
    return BackendContext(
        repository=backend,
        settings_store=DesktopSettings(backend, credentials),
        exports=backend,
        telemetry=services,
        storage_for=backend.storage_for,
        build_client=lambda config: build_local_client(config, credentials),
        enqueue=services.enqueue,
        data_dir=str(backend.workspace / "projects"),
        project_dir=lambda pid: str(backend.project_dir(pid)),
        health=services.health,
        update_term=lambda pid, storage, source, term: storage.update_term(source, term),
        api_token=api_token,
    )


def create_app(
    workspace: str | Path | None = None,
    *,
    context: BackendContext | None = None,
    api_token: str | None = None,
):
    from .desktop import ORIGINS
    from .routers import credentials, desktop_export

    if context is None:
        if workspace is None:
            raise ValueError("Desktop requires an explicit workspace")
        context = create_context(workspace, api_token=api_token)
    services = context.telemetry

    @asynccontextmanager
    async def lifespan(app):
        with use_context(context):
            runtime = LocalRuntime(context.repository, context)
            services.runtime = runtime
            try:
                await runtime.start()
                yield
            finally:
                try:
                    await runtime.stop()
                finally:
                    services.runtime = None
                    context.settings_store.credentials.clear_session()
                    context.repository.close()

    app = create_http_app(context, lifespan=lifespan, origins=list(ORIGINS))
    app.include_router(credentials.router)
    app.include_router(desktop_export.router)
    app.include_router(updates_router)
    app.state.update_guard = UpdateGuard(services)
    app.add_middleware(
        UpdateAdmissionMiddleware, guard=app.state.update_guard, token=context.api_token
    )
    return app
