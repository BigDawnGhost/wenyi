"""Web assembly. Public entry: uvicorn wenyi_api.main:app."""

import os
from contextlib import asynccontextmanager

from wenyi_backend.application import create_app as create_http_app
from wenyi_backend.context import BackendContext, use_context

from . import __version__
from .adapters import create_context, postgres_repository
from .config import Settings, settings


def create_app(config: Settings | None = None, *, context: BackendContext | None = None):
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
    application.version = __version__
    application.description = "Web API and background workers for Wenyi's translation engine."
    return application


app = create_app()
