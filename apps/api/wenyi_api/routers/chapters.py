"""章节列表与段落（原文/译文对照）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import dal
from ..db import get_pool
from ..schemas import ChapterSegments, ChapterSummary
from ..storage_pg import PostgresStorage

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
