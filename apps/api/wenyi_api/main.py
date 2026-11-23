"""Web assembly. Public entry: uvicorn wenyi_api.main:app."""

import os
from contextlib import asynccontextmanager
from typing import cast

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from starlette.types import ExceptionHandler
from wenyi_backend.application import create_app as create_http_app
from wenyi_backend.context import BackendContext, use_context
from wenyi_core.observability import configure_logging

from . import __version__
from .adapters import create_context, postgres_repository
from .config import Settings, settings

# A saturated database pool is transient backpressure, not a server fault.
POOL_TIMEOUT_RETRY_AFTER = "5"


async def pool_timeout_handler(request: Request, exc: PoolTimeout) -> JSONResponse:
    """Answer pool saturation with 503 and a retry hint instead of a raw 500."""
    return JSONResponse(
        {"detail": "Database connection pool is busy; please retry shortly."},
        status_code=503,
        headers={"Retry-After": POOL_TIMEOUT_RETRY_AFTER},
    )


def create_app(config: Settings | None = None, *, context: BackendContext | None = None):
    configure_logging(service="wenyi-api")
    context = context or create_context(config or settings)

    @asynccontextmanager
    async def lifespan(app):
        with use_context(context):
            repository = postgres_repository(context)
            repository.start()
            try:
                yield
            finally:
                repository.close()

    application = create_http_app(
        context,
        lifespan=lifespan,
        origins=os.environ.get("WENYI_CORS_ORIGINS", "*").split(","),
    )
    application.add_exception_handler(PoolTimeout, cast(ExceptionHandler, pool_timeout_handler))
    application.version = __version__
    application.description = "Web API and background workers for Wenyi's translation engine."
    return application


app = create_app()
