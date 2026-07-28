"""章节列表与段落（原文/译文对照）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import dal
from ..db import get_pool
from ..schemas import ChapterSegments, ChapterSummary, JobEnqueued
from ..storage_pg import PostgresStorage
from ..workers import enqueue

router = APIRouter(prefix="/projects/{pid}/chapters", tags=["chapters"])


@router.get("", response_model=list[ChapterSummary])
def list_chapters(pid: str) -> list[dict]:
    return dal.chapter_summaries(pid)


@router.get("/{ci}", response_model=ChapterSegments)
def get_chapter(pid: str, ci: int) -> dict:
    storage = PostgresStorage(pid, get_pool())
    try:
        ch = storage.load_chapter(ci)
    except KeyError:
        raise HTTPException(404, "chapter not found") from None
    review_issues = (ch.meta or {}).get("review_issues", [])
    return {
        "index": ch.index, "title": ch.title,
        "title_translated": ch.title_translated,
        "segments": [
            {"index": s.index, "source": s.source, "target": s.target, "kind": s.kind}
            for s in ch.segments
        ],
        "review_issues": review_issues,
    }


@router.post("/{ci}/translate", response_model=JobEnqueued)
async def translate_chapter(pid: str, ci: int) -> dict:
    project = dal.get_project(pid)
    if project is None:
        raise HTTPException(404, "project not found")
    if project["status"] in dal.RUNNING_PROJECT_STATUSES:
        raise HTTPException(409, "project already has a running task")

    chapter = next(
        (item for item in dal.chapter_summaries(pid) if item["index"] == ci),
        None,
    )
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    if chapter["status"] == "done":
        raise HTTPException(409, "chapter is already translated")
    if chapter["status"] == "translating":
        raise HTTPException(409, "chapter is already translating")

    dal.set_project_status(pid, "translating")
    dal.set_chapter_status(pid, ci, "translating")
    try:
        job = await enqueue(
            "run_chapter_translation",
            project_id=pid,
            chapter_index=ci,
        )
    except Exception:
        dal.set_project_status(pid, project["status"])
        dal.set_chapter_status(pid, ci, chapter["status"])
        raise
    return {
        "job_id": job.job_id if job else "sync",
        "project_id": pid,
        "kind": "chapter_translation",
    }
