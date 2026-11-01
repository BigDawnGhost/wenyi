"""Assemble the shared HTTP contract with explicit application-owned ports."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .context import BackendContext, ContextMiddleware
from .errors import ApiError, api_error_handler, http_exception_handler
from .request_context import RequestContextMiddleware
from .routers import (
    chapters,
    configuration,
    events,
    export,
    glossary,
    health,
    metrics,
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
    # Codes are additive: every error keeps its previous "detail" text as well.
    # Starlette resolves handlers by exact class, so register the base exception
    # to also cover fastapi.HTTPException raised by the routers.
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)

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
    # Added last, so it is the outermost layer and can stamp every response.
    app.add_middleware(RequestContextMiddleware)
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
        metrics,
    ):
        app.include_router(module.router)
    return app
