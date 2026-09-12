"""Call-local translation batching, alignment, fallback, and glossary stages."""

from __future__ import annotations

from dataclasses import replace

from trans_novel.epub.slots import (
    distribute_slot_translation,
    normalized_source_text,
    source_passthrough_transport,
)
from trans_novel.ingest.models import KIND_HEADING, Segment
from trans_novel.llm.errors import LLM_FALLBACK_ERRORS
from trans_novel.pipeline.nodes.common import source_context_before
from trans_novel.pipeline.nodes.glossary import extract_and_store, store_extracted_terms
from trans_novel.pipeline.state import RollingContext
from trans_novel.postprocess.punct import normalize_heading_numbering


def align_epub_translations(segments, translations: list[str]) -> list[object]:
    """Distribute complete translations across EPUB slots deterministically."""
    result: list[object] = list(translations)
    for index, (segment, translation) in enumerate(zip(segments, translations, strict=True)):
        if segment.epub_state is None:
            continue
        complete = (
            normalize_heading_numbering(translation) if segment.kind == "heading" else translation
        )
        result[index] = (
            source_passthrough_transport(segment.epub_state)
            if complete == segment.source
            else distribute_slot_translation(segment.epub_state, complete)
        )
    return result


def safe_batch_fallback(batch) -> tuple[list[object], int]:
    return [
        source_passthrough_transport(segment.epub_state)
        if segment.epub_state is not None
        else segment.source
        for segment in batch
    ], 0


def translate_batch(
    translator,
    batch,
    terms,
    context: RollingContext,
    style: str,
    *,
    chapter_segments: list[Segment],
    start_index: int,
    chapter_title: str,
    n_recent: int,
    single_segment_translation: bool = True,
) -> tuple[list[object], int]:
    """Translate one ordinary batch, preserving per-heading prompt semantics."""
    for segment in batch:
        if segment.epub_state is not None and segment.source != normalized_source_text(
            segment.epub_state.slots
        ):
            raise ValueError(f"EPUB source slot coverage mismatch: {segment.resource_href}")
    local_context = replace(context, recent_targets=list(context.recent_targets))
    try:
        if single_segment_translation:
            translated: list[str] = []
            request_count = 0
            for offset, segment in enumerate(batch, start_index):
                result = translator.translate_batch(
                    [segment.source],
                    agent="analyst" if segment.kind == KIND_HEADING else "translator",
                    operation=(
                        "translate.heading" if segment.kind == KIND_HEADING else "translate.single"
                    ),
                    fallback_agent=None if segment.kind == KIND_HEADING else "analyst",
                    glossary_terms=terms,
                    style=style if segment.kind != KIND_HEADING else "",
                    context=local_context.render(n_recent) if segment.kind != KIND_HEADING else "",
                    source_context=(
                        source_context_before(chapter_segments, offset)
                        if segment.kind != KIND_HEADING
                        else ""
                    ),
                    chapter_title=chapter_title if segment.kind != KIND_HEADING else "",
                    kind=KIND_HEADING if segment.kind == KIND_HEADING else None,
                )
                translated.extend(result.translations)
                local_context.add_targets(list(result.translations))
                request_count += result.request_count
        else:
            result = translator.translate_batch(
                [s.source for s in batch],
                agent="translator",
                glossary_terms=terms,
                style=style,
                context=local_context.render(n_recent),
                source_context=source_context_before(chapter_segments, start_index),
                chapter_title=chapter_title,
            )
            translated = list(result.translations)
            request_count = result.request_count
        return align_epub_translations(batch, translated), request_count
    except LLM_FALLBACK_ERRORS:
        return safe_batch_fallback(batch)


def extract_batch_glossary(
    extractor, glossary, store, chapter: int, start_index: int, batch, existing_terms=None
):
    """Extract and persist one batch's terms, including its event."""
    src_text = "\n".join(s.source for s in batch)
    tgt_text = "\n".join(s.target or "" for s in batch)
    if existing_terms is None:
        summary, changed = extract_and_store(extractor, glossary, src_text, tgt_text, chapter)
    else:
        terms = extractor.extract(src_text, tgt_text, existing_terms)
        summary, changed = store_extracted_terms(glossary, terms, chapter)
    store.log_event(
        "batch_glossary_extracted",
        chapter=chapter,
        start_index=start_index,
        count=len(batch),
        summary=summary,
    )
    return summary, changed


__all__ = [
    "align_epub_translations",
    "extract_batch_glossary",
    "safe_batch_fallback",
    "translate_batch",
]
