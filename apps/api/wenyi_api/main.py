"""Assemble FastAPI routes, the database pool, optional token authentication and CORS.

Start with ``uvicorn wenyi_api.main:app --reload``.
OpenAPI is available at ``/docs`` and ``/openapi.json`` and defines frontend types.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import settings
from .db import close_pool, init_pool
from .routers import (
    chapters,
    configuration,
    events,
    export,
    glossary,
    health,
    projects,
    report,
    review,
    strategies,
    style,
    subtitles,
    ws,
)
from .routers import (
    settings as global_settings,
)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        init_pool(settings.psycopg_dsn)
        yield
        close_pool()

    app = FastAPI(
        lifespan=lifespan,
        title="Wenyi API",
        version=__version__,
        description="Web API and background workers for Wenyi's translation engine.",
    )

    # Optional HTTP token authentication; health checks are public and WebSocket auth is separate.
    if settings.api_token:
        from fastapi import Request
        from fastapi.responses import JSONResponse
        from starlette.middleware.base import BaseHTTPMiddleware

        token = settings.api_token

        class _TokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                path = request.url.path
                if path.startswith("/health") or path.startswith("/ws/"):
                    return await call_next(request)
                auth = request.headers.get("authorization", "")
                provided = auth.removeprefix("Bearer ").strip()
                if provided != token:
                    return JSONResponse({"detail": "invalid api token"}, status_code=401)
                return await call_next(request)

        app.add_middleware(_TokenMiddleware)

    # CORS wraps authentication so preflights and error responses reach browser clients.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("WENYI_CORS_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(strategies.router)
    app.include_router(projects.router)
    app.include_router(configuration.router)
    app.include_router(global_settings.router)
    app.include_router(report.router)
    app.include_router(subtitles.router)
    app.include_router(chapters.router)
    app.include_router(glossary.router)
    app.include_router(review.router)
    app.include_router(style.router)
    app.include_router(export.router)
    app.include_router(events.router)
    app.include_router(ws.router)

    return app


app = create_app()
