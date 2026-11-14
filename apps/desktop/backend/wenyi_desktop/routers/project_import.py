"""Adopt an existing project directory into this library, or say why it cannot be."""

from __future__ import annotations

from pathlib import Path, PurePath

from fastapi import APIRouter
from pydantic import BaseModel
from wenyi_backend.context import current_context
from wenyi_backend.errors import ApiError

from ..local_backend import LocalBackend
from ..project_import import ImportRefused, import_project

router = APIRouter(prefix="/desktop/projects", tags=["desktop-import"])

#: One code for every refusal, because the distinction that matters to a person
#: is the sentence, and `ImportRefused` already carries it. The status separates
#: "this folder is not a project" from "it is one, and it is already here".
IMPORT_FAILED = "project_import_failed"
IMPORT_CONFLICT = "project_import_conflict"

_CONFLICTS = {"project_already_registered", "project_destination_exists"}


class ImportRequest(BaseModel):
    path: str


def _backend() -> LocalBackend:
    return current_context().repository


@router.post("/import", status_code=201)
def import_existing_project(body: ImportRequest) -> dict:
    """Turn a folder the user picked into a catalog project.

    The path arrives from a native folder picker, so it is a real path rather
    than an upload. Only the folder's own state is read: nothing is copied in
    before the scan proves the folder is a project, which is what keeps a wrong
    pick from leaving anything behind.
    """
    raw = body.path.strip()
    if not raw or "\x00" in raw:
        raise ApiError(IMPORT_FAILED, 422, "That folder cannot be used.")
    try:
        directory = Path(raw)
    except (OSError, ValueError) as error:
        raise ApiError(IMPORT_FAILED, 422, "That folder cannot be used.") from error
    if not directory.is_absolute() or PurePath(raw).name in {"", ".", ".."}:
        raise ApiError(IMPORT_FAILED, 422, "Choose a folder on this computer.")
    try:
        return import_project(_backend(), directory)
    except ImportRefused as error:
        status = 409 if error.reason in _CONFLICTS else 422
        raise ApiError(
            IMPORT_CONFLICT if status == 409 else IMPORT_FAILED, status, error.message
        ) from error
