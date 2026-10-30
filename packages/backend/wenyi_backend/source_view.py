"""Read-only source projections before model-assisted initialization commits."""

from __future__ import annotations

from wenyi_core.ingest.models import Document
from wenyi_core.pipeline.input_preparation import load_parsed_document
from wenyi_core.storage.protocol import Storage

from . import dal
from .project_service import effective_config, read_storage_for


def parsed_source(project: dict, storage: Storage) -> Document | None:
    """Read the immutable upload's matching parse, never staged translation state."""
    source = project.get("source_path")
    digest = project.get("source_sha256")
    if not source or not digest or project.get("fmt") == "srt":
        return None
    # Uploads have content-bound identities; HTTP reads need not rehash the whole book.
    return load_parsed_document(storage, source, effective_config(project), actual_sha256=digest)


def book_preview(document: Document, fmt: str) -> dict:
    chapters = [
        {"index": chapter.index, "title": chapter.title, "word_count": len(chapter.text_segments)}
        for chapter in document.chapters
    ]
    return {
        "title": document.title,
        "fmt": fmt,
        "chapter_count": len(chapters),
        "total_word_count": sum(chapter["word_count"] for chapter in chapters),
        "source_lang": document.source_lang,
        "chapters": chapters,
    }


def chapter_summaries(project: dict) -> list[dict]:
    if project.get("initialized"):
        return dal.chapter_summaries(project["id"])
    storage = read_storage_for(project["id"])
    try:
        document = parsed_source(project, storage)
        if document is None:
            return []
        return [
            {
                "index": chapter.index,
                "title": chapter.title,
                "title_translated": None,
                "status": "pending",
                "word_count": len(chapter.text_segments),
                "target_word_count": 0,
                "review_issue_count": 0,
                "review_status": "pending",
            }
            for chapter in document.chapters
        ]
    finally:
        storage.close()
