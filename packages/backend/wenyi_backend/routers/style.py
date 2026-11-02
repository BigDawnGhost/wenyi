"""Book analysis and chapter digest inspection/editing."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter

from ..errors import CHAPTER_NOT_FOUND, ApiError
from ..schemas import AnalysisOut, AnalysisUpdate, DigestUpdate
from .dependencies import project_write, require_project, storage_for

router = APIRouter(prefix="/projects/{pid}", tags=["style"])


@router.get("/analysis", response_model=AnalysisOut)
def get_analysis(pid: str) -> dict:
    require_project(pid)
    with closing(storage_for(pid)) as storage:
        digests = []
        for entry in storage.load_manifest().get("chapters", []):
            chapter = storage.load_chapter(entry["index"])
            digests.append(
                {
                    "index": chapter.index,
                    "title": chapter.title,
                    "digest": str(chapter.meta.get("source_digest") or ""),
                }
            )
        return {"analysis": storage.load_analysis() or {}, "chapter_digests": digests}


@router.put("/analysis")
def update_analysis(pid: str, body: AnalysisUpdate) -> dict:
    with project_write(pid) as (_project, storage):
        storage.save_analysis(body.analysis)
        storage.log_event("analysis_edited")
    return {"ok": True}


@router.put("/chapter-digests/{ci}")
def update_chapter_digest(pid: str, ci: int, body: DigestUpdate) -> dict:
    with project_write(pid) as (_project, storage):
        try:
            chapter = storage.load_chapter(ci)
        except KeyError:
            raise ApiError(CHAPTER_NOT_FOUND, 404, "chapter not found") from None
        chapter.meta["source_digest"] = body.digest
        storage.save_chapter(chapter)
        storage.log_event("chapter_digest_edited", chapter=ci)
    return {"ok": True}
