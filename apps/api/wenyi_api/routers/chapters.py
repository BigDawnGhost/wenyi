"""Book chapter inspection, title editing and resumable chapter translation."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import dal
from ..job_service import start_job
from ..project_service import project_write, require_book, require_project, storage_for
from ..schemas import (
    ChapterSegments,
    ChapterSummary,
    ChapterTitleOut,
    ChapterTitleUpdate,
    JobEnqueued,
)

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


def _linked_toc_entries(manifest: dict, chapter: dict) -> list[dict]:
    """Match the bound node and equivalent NAV/NCX labels by exact source destination."""
    entry_id = chapter.get("toc_entry_id")
    if not entry_id:
        return []
    entries = [
        entry
        for entry in (manifest.get("meta") or {}).get("toc_entries", [])
        if isinstance(entry, dict)
    ]
    primary = next((entry for entry in entries if entry.get("entry_id") == entry_id), None)
    if primary is None:
        return []
    target_key = primary.get("target_key")
    return [
        entry
        for entry in entries
        if entry is primary
        or (
            target_key
            and not primary.get("external")
            and not entry.get("external")
            and entry.get("target_key") == target_key
            and entry.get("title") == primary.get("title")
        )
    ]


@router.put("/{ci}/title", response_model=ChapterTitleOut)
def update_chapter_title(pid: str, ci: int, body: ChapterTitleUpdate) -> dict:
    with project_write(pid) as (project, storage):
        require_book(project)
        if not storage.exists():
            raise HTTPException(409, "Prepare the book before editing chapter titles")
        with storage.state_lock():
            manifest = storage.load_manifest()
            chapter = next(
                (row for row in manifest.get("chapters", []) if row["index"] == ci), None
            )
            if chapter is None:
                raise HTTPException(404, "chapter not found")
            before = chapter.get("title_translated")
            if before != body.expected_title_translated:
                raise HTTPException(
                    409, "This title changed; reload its latest value before saving"
                )
            linked = _linked_toc_entries(manifest, chapter)
            changes = [
                {"entry_id": entry.get("entry_id"), "before": entry.get("title_translated")}
                for entry in linked
                if entry.get("title_translated") != body.title_translated
            ]
            if before != body.title_translated or changes:
                chapter["title_translated"] = body.title_translated
                for entry in linked:
                    entry["title_translated"] = body.title_translated
                storage.save_manifest(manifest)
                storage.log_event(
                    "chapter_title_edited",
                    chapter=ci,
                    before=before,
                    after=body.title_translated,
                    toc_changes=changes,
                )
    return {"index": ci, "title_translated": body.title_translated}


@router.post("/{ci}/translate", response_model=JobEnqueued)
async def translate_chapter(pid: str, ci: int) -> dict:
    require_book(require_project(pid))
    chapter = next((row for row in dal.chapter_summaries(pid) if row["index"] == ci), None)
    if chapter is None:
        raise HTTPException(404, "chapter not found")
    if chapter["status"] == "done":
        raise HTTPException(409, "chapter is already translated")
    return await start_job(pid, "chapter_translation", params={"chapter_index": ci})
