"""Choose where a new project lives, and refuse a folder the layout cannot use."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from wenyi_backend.context import current_context
from wenyi_backend.errors import ApiError

from ..library_location import LibraryLocationRefused, admit
from ..local_backend import LocalBackend

router = APIRouter(prefix="/desktop/library", tags=["desktop-library"])

#: One code for every refusal, because the distinction that matters to a person
#: is the sentence, and `LibraryLocationRefused` already carries the reason.
LOCATION_REFUSED = "project_location_refused"


class LocationRequest(BaseModel):
    path: str


class LocationAnswer(BaseModel):
    #: The absolute folder a project created here would be written into.
    path: str


def _backend() -> LocalBackend:
    return current_context().repository


@router.post("/admit", response_model=LocationAnswer)
def admit_location(body: LocationRequest) -> dict:
    """Say whether a folder may hold a project, before the person commits to it.

    The check runs when a project is created too; asking first is what lets the
    interface say why a folder cannot be used while the picker is still open,
    instead of after an upload that then has to be undone.
    """
    try:
        location = admit(body.path, _backend())
    except LibraryLocationRefused as error:
        raise ApiError(LOCATION_REFUSED, 422, error.message) from error
    return {"path": str(location)}
