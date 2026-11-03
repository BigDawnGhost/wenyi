"""Say what the last startup repair found, so the interface can report it.

The scan runs before any request exists, so its result has to be asked for
rather than pushed. This route is the ask: the project list is the first page
the shared UI renders, and a notice about projects that came back -- and folders
that did not -- belongs beside it."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from wenyi_backend.context import current_context

router = APIRouter(prefix="/desktop/recovery", tags=["desktop-recovery"])


class SkippedFolder(BaseModel):
    #: The folder as the person sees it. Not a project path: it never had one.
    name: str
    #: The stable code, so a client can branch without parsing the sentence.
    reason: str
    #: The sentence the import dialog would show for the same folder.
    message: str


class RecoveryAnswer(BaseModel):
    #: Projects the repair rebuilt. Zero is the ordinary case: no repair ran.
    recovered: int = 0
    skipped: list[SkippedFolder] = []


@router.get("/last", response_model=RecoveryAnswer)
def last_recovery() -> dict:
    """What the startup scan decided during this launch.

    Scoped to one launch on purpose. The next start scans again and reaches its
    own conclusion, so a notice about a folder the person has since acted on
    would be stale -- and a stale notice about a working folder is worse than
    none, because it sends them looking for a problem that is gone.
    """
    runtime = current_context().telemetry.runtime
    report = getattr(runtime, "recovery_report", None) if runtime is not None else None
    return report or {"recovered": 0, "skipped": []}
