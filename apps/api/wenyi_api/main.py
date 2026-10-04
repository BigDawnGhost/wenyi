"""Web assembly. Public entry: uvicorn wenyi_api.main:app."""

import os
from contextlib import asynccontextmanager

from wenyi_backend.application import create_app as create_http_app
from wenyi_backend.context import BackendContext, use_context

from . import __version__
from .adapters import create_context
from .config import Settings, settings


def create_app(config: Settings | None = None, *, context: BackendContext | None = None):
    context = context or create_context(config or settings)

    @asynccontextmanager
    async def lifespan(app):
        with use_context(context):
            context.repository.start()
            try:
                yield
            finally:
                context.repository.close()

    application = create_http_app(
        context,
        lifespan=lifespan,
        origins=os.environ.get("WENYI_CORS_ORIGINS", "*").split(","),
    )
    application.version = __version__
    application.description = "Web API and background workers for Wenyi's translation engine."
    return application


app = create_app()
