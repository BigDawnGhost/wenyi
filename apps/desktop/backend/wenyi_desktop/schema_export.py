"""Build the Desktop-only HTTP contract offline, without a workspace or credentials.

The Desktop shell adds routes to the shared application: credential management,
native save plans, plugin controls, project import and library admission. Those
routes are part of the contract the Desktop frontend calls, so they are generated
rather than hand-maintained. They live in this package because `wenyi_backend`
must never import a platform adapter; the shared routes stay in
`wenyi_backend.schema_export` and the two documents stay separate files.
"""

from __future__ import annotations

from fastapi import FastAPI


def build_schema() -> dict:
    """Describe the Desktop-added routes only; the shared contract is its own file."""
    from .routers import (
        babeldoc_plugin,
        credentials,
        desktop_export,
        external_credentials,
        library_location,
        project_import,
        recovery,
    )
    from .updates import router as updates_router

    # The routers only declare routes; every handler resolves its dependencies
    # at request time, so assembling them needs no workspace, token or store.
    app = FastAPI(title="Wenyi Desktop API", version="0.0.0")
    for module in (
        credentials,
        desktop_export,
        external_credentials,
        babeldoc_plugin,
        project_import,
        library_location,
        recovery,
    ):
        app.include_router(module.router)
    app.include_router(updates_router)
    return app.openapi()
