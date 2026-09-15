"""Book chapter inspection and resumable chapter translation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import dal
from ..job_service import start_job
from ..project_service import require_book, require_project, storage_for
from ..schemas import ChapterSegments, ChapterSummary, JobEnqueued

router = APIRouter(prefix="/projects/{pid}/chapters", tags=["chapters"])


def chapter_payload(storage, ci: int) -> dict:
    try:
        chapter = storage.load_chapter(ci)
    except KeyError:
        raise HTTPException(404, "chapter not found") from None
    review = storage.load_latest_review_result() or {}
    text_segments = chapter.text_segments
    issues = []
    current_review = dal.chapter_review_state(review, chapter.meta)[1]
    for issue in (review.get("issues") or []) if current_review else []:
        if issue.get("chapter") != ci:
            continue
        position = issue.get("index")
        if (
            isinstance(position, int)
            and not isinstance(position, bool)
            and 0 <= position < len(text_segments)
        ):
            issues.append(
                {**issue, "text_position": position, "index": text_segments[position].index}
            )
    return {
        "index": chapter.index,
        "title": chapter.title,
        "title_translated": getattr(chapter, "title_translated", None)
        or chapter.meta.get("title_translated"),
        "segments": [
            {
                "index": segment.index,
                "source": segment.source,
                "target": segment.target,
                "target_before_polish": segment.target_before_polish,
                "kind": segment.kind,
                "anchor": segment.anchor,
            }
            for segment in chapter.segments
        ],
        "review_issues": issues,
    }


@router.get("", response_model=list[ChapterSummary])
def list_chapters(pid: str) -> list[dict]:
    require_book(require_project(pid))
    return dal.chapter_summaries(pid)


@router.get("/{ci}", response_model=ChapterSegments)
def get_chapter(pid: str, ci: int) -> dict:
    require_book(require_project(pid))
    return chapter_payload(storage_for(pid), ci)


@router.post("/{ci}/translate", response_model=JobEnqueued)
async def translate_chapter(pid: str, ci: int) -> dict:
    require_book(require_project(pid))
    chapter = next((row for row in dal.chapter_summaries(pid) if row["index"] == ci), None)
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    if chapter["status"] == "done":
        raise HTTPException(409, "chapter is already translated")
    return await start_job(pid, "chapter_translation", params={"chapter_index": ci})
