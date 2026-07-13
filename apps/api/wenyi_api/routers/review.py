"""审校：并排段落对照（P0 只读 + 单段编辑译文）+ 标记完成。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from wenyi_core.ingest.models import Segment

from ..db import get_pool
from ..storage_pg import PostgresStorage

router = APIRouter(prefix="/projects/{pid}/review", tags=["review"])


class SegmentEdit(BaseModel):
    target: str


class ReviewStatus(BaseModel):
    status: str  # ok | pending


@router.get("/{ci}")
def get_chapter_for_review(pid: str, ci: int) -> dict:
    storage = PostgresStorage(pid, get_pool())
    try:
        ch = storage.load_chapter(ci)
    except KeyError:
        raise HTTPException(404, "chapter not found") from None
    review_issues = (ch.meta or {}).get("review_issues", [])
    return {
        "index": ch.index, "title": ch.title,
        "segments": [
            {"index": s.index, "source": s.source, "target": s.target,
             "kind": s.kind, "anchor": s.anchor}
            for s in ch.segments
        ],
        "review_issues": review_issues,
    }


@router.put("/{ci}/segments/{seg_idx}")
def edit_segment(pid: str, ci: int, seg_idx: int, body: SegmentEdit) -> dict:
    """编辑单段译文（写回 chapters/segments）。"""
    storage = PostgresStorage(pid, get_pool())
    try:
        ch = storage.load_chapter(ci)
    except KeyError:
        raise HTTPException(404, "chapter not found") from None
    seg: Segment | None = next((s for s in ch.segments if s.index == seg_idx), None)
    if seg is None:
        raise HTTPException(404, "segment not found")
    seg.target = body.target
    storage.save_chapter(ch)
    return {"ok": True, "index": seg_idx}


@router.post("/{ci}/complete")
def mark_reviewed(pid: str, ci: int) -> dict:
    """标记该章审校完成（在 chapter.meta 里记 review_passed=True）。"""
    storage = PostgresStorage(pid, get_pool())
    try:
        ch = storage.load_chapter(ci)
    except KeyError:
        raise HTTPException(404, "chapter not found") from None
    meta = dict(ch.meta or {})
    meta["review_passed"] = True
    ch.meta = meta
    storage.save_chapter(ch)
    return {"ok": True, "chapter": ci, "review_passed": True}
