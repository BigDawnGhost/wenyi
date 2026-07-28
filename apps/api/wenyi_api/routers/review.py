"""审校：并排段落对照（P0 只读 + 单段编辑译文）+ 标记完成 + 全书 AI 审校触发。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from wenyi_core.ingest.models import Segment

from ..db import get_pool
from ..schemas import JobEnqueued
from ..storage_pg import PostgresStorage
from ..workers import enqueue

router = APIRouter(prefix="/projects/{pid}/review", tags=["review"])


class SegmentEdit(BaseModel):
    target: str


class ReviewStatus(BaseModel):
    status: str  # ok | pending


class ReviewRunRequest(BaseModel):
    force: bool = False
    autofix: bool = True


# 静态路径须注册在 /{ci} 之前，避免被路径参数抢先匹配。
@router.post("/run", response_model=JobEnqueued)
async def run_ai_review(pid: str, body: ReviewRunRequest | None = None) -> dict:
    """触发全书 AI 审校：按章并发检测问题，严重项自动修复。"""
    from .. import dal
    if dal.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    dal.set_project_status(pid, "reviewing")
    job = await enqueue(
        "run_review",
        project_id=pid,
        force=body.force if body else False,
        autofix=body.autofix if body else True,
    )
    return {"job_id": job.job_id if job else "sync", "project_id": pid, "kind": "review"}


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
