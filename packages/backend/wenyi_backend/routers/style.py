"""Book analysis and chapter digest inspection/editing."""

from __future__ import annotations

from contextlib import closing

from fastapi import APIRouter, HTTPException
from wenyi_core.storage.chapter_reading import ChapterNotFound

from ..errors import CHAPTER_NOT_FOUND, ApiError
from ..schemas import AnalysisOut, AnalysisUpdate, DigestUpdate
from .dependencies import project_write, require_project, storage_for

router = APIRouter(prefix="/projects/{pid}", tags=["style"])


@router.get("/analysis", response_model=AnalysisOut)
def get_analysis(pid: str) -> dict:
    require_project(pid)
    # Preserve the project-open checks: an unavailable location is not unprepared.
    with closing(storage_for(pid)) as storage:
        if not storage.exists():
            raise HTTPException(409, "Prepare the book before viewing the analysis")
        try:
            digests = storage.read_chapter_digests()
        except ChapterNotFound as error:
            raise ApiError(CHAPTER_NOT_FOUND, 404, str(error)) from None
        return {
            "analysis": storage.load_analysis() or {},
            "chapter_digests": [
                {"index": index, "title": title, "digest": str(digest or "")}
                for index, title, digest in digests
            ],
        }


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
        except (KeyError, FileNotFoundError):
            raise ApiError(CHAPTER_NOT_FOUND, 404, "chapter not found") from None
        chapter.meta["source_digest"] = body.digest
        storage.save_chapter(chapter)
        storage.log_event("chapter_digest_edited", chapter=ci)
    return {"ok": True}
