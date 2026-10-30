"""Assemble the shared HTTP contract with explicit application-owned ports."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .context import BackendContext, ContextMiddleware
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
    settings,
    strategies,
    style,
    subtitles,
    ws,
)


def create_app(
    context: BackendContext, *, lifespan=None, origins: list[str] | None = None
) -> FastAPI:
    app = FastAPI(lifespan=lifespan, title="Wenyi API", version=__version__)
    app.state.backend = context

    if context.api_token:

        class TokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                path = request.url.path
                if not path.startswith(("/health", "/ws/")):
                    provided = (
                        request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                    )
                    if provided != context.api_token:
                        return JSONResponse({"detail": "invalid api token"}, status_code=401)
                return await call_next(request)

        app.add_middleware(TokenMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins is not None else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )
    app.add_middleware(ContextMiddleware, context=context)
    for module in (
        health,
        strategies,
        projects,
        configuration,
        settings,
        report,
        subtitles,
        chapters,
        glossary,
        review,
        style,
        export,
        events,
        ws,
    ):
        app.include_router(module.router)
    return app
