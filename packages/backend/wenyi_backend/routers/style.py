"""Book analysis and chapter digest inspection/editing."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..project_service import project_write, require_book, require_project, storage_for
from ..schemas import AnalysisOut, AnalysisUpdate, DigestUpdate

router = APIRouter(prefix="/projects/{pid}", tags=["style"])


@router.get("/analysis", response_model=AnalysisOut)
def get_analysis(pid: str) -> dict:
    require_book(require_project(pid))
    storage = storage_for(pid)
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
    with project_write(pid) as (project, storage):
        require_book(project)
        storage.save_analysis(body.analysis)
        storage.log_event("analysis_edited")
    return {"ok": True}


@router.put("/chapter-digests/{ci}")
def update_chapter_digest(pid: str, ci: int, body: DigestUpdate) -> dict:
    with project_write(pid) as (project, storage):
        require_book(project)
        try:
            chapter = storage.load_chapter(ci)
        except KeyError:
            raise HTTPException(404, "chapter not found") from None
        chapter.meta["source_digest"] = body.digest
        storage.save_chapter(chapter)
        storage.log_event("chapter_digest_edited", chapter=ci)
    return {"ok": True}
