"""App-owned HTTP admission guard for native Desktop updates."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.responses import JSONResponse

from .routers.credentials import desktop_only

CONTROL_PATHS = frozenset({"/desktop/updates/prepare", "/desktop/updates/cancel"})


class UpdateGuard:
    """Serialize control requests; admission changes never cross an await."""

    def __init__(self, services):
        self.services = services
        self.closed = False
        self.inflight = 0
        self._control = asyncio.Lock()

    def runtime(self):
        runtime = self.services.runtime
        if runtime is None or not runtime.accepting:
            raise HTTPException(503, "Desktop workers are unavailable")
        return runtime

    async def prepare(self) -> None:
        async with self._control:
            runtime = self.runtime()
            if self.closed:
                return
            self.closed = True
            committed = False
            try:
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

    async def cancel(self) -> None:
        # A cancel arriving during the check waits for that check to finish. It
        # cannot reopen admission while another prepare is inspecting the catalog.
        async with self._control:
            self.runtime()
            self.closed = False


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
