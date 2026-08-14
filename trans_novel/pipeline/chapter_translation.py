"""Legacy-only chapter translation coordination.

This module owns the old ``RunStore`` chapter/batch state machine.  It is kept
inside :mod:`trans_novel.pipeline` deliberately: old tasks may import and run it
without importing the new workflow, graph, or LangGraph translation runtime.

Durability is defined by the order of side effects below.  In particular, body
text is saved before annotation/glossary work, glossary extraction follows the
translated-batch event, and the final chapter file and manifest status are
published together before the ``chapter_done`` event is appended.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..glossary.extractor import TranslatedSegmentEvidence
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.models import Chapter, Segment
from ..postprocess.punct import normalize_zh_segments
from ..services.translation_batches import plan_resumable_batches
from .context import RollingContext
from .runstore import STATUS_DONE, RunStore

ProgressFn = Callable[[int, int, str], None]


@dataclass(frozen=True, slots=True)
class TranslationPolicy:
    """Immutable legacy configuration used by one chapter translation call."""

    max_chars_per_batch: int


@dataclass
class BatchResult:
    """Translated targets plus the source/target pairs selected for backtranslation."""

    targets: list[str]
    bt_samples: list[tuple[str, str]] = field(default_factory=list)


# Callback types describe the compatibility seams injected by ``Orchestrator``.
# Keeping them explicit prevents this legacy module from holding an orchestrator,
# constructing agents, or reaching into the new workflow runtime.
ProcessBatchFn = Callable[..., BatchResult]
PlanBatchesFn = Callable[[list[Segment], int], list[list[Segment]]]
ReportProgressFn = Callable[..., None]
TermSnapshotFn = Callable[[GlossaryStore, list[Segment]], list[GlossaryTerm]]
ExtractBatchGlossaryFn = Callable[..., dict[str, int]]
AlignAfterBatchFn = Callable[[int, Chapter, int, int, RunStore], None]
SyncContextFn = Callable[[RollingContext, list[Segment], int], None]
UpdateHistoryFn = Callable[
    [dict[tuple[int, int], TranslatedSegmentEvidence], int, int, list[Segment]],
    None,
]
AnnotationContextsFn = Callable[
    [list[Segment], dict[str, Any] | None],
    list[list[dict[str, str]]],
]
ChapterLabelFn = Callable[[str, int], str]
ExtractChapterGlossaryFn = Callable[..., dict[str, int]]
BacktranslationCheckFn = Callable[[list[str], list[str]], list[dict[str, Any]]]
TranslateBatchFn = Callable[..., list[str]]
PolishBatchFn = Callable[..., list[str]]
RandomSampleFn = Callable[[], float]
FeatureSwitch = Callable[[], bool]
ContextWindowFn = Callable[[], int]
SampleRateFn = Callable[[], float]


def report_translation_progress(
    progress: ProgressFn | None,
    *,
    chapter_done: int,
    chapter_total: int,
    overall_done: int,
    overall_total: int,
    label: str,
) -> None:
    """Report nested Rich progress while preserving the legacy callback shape."""

    if progress is None:
        return
    nested_update = getattr(progress, "update_translation", None)
    if callable(nested_update):
        nested_update(
            chapter_done,
            chapter_total,
            overall_done,
            overall_total,
            label,
        )
        return
    progress(overall_done, overall_total, label)


def resume_legacy_batches(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """Plan budgeted batches and split each one at translated/pending boundaries."""

    return [
        segments[plan.start_index : plan.stop_index]
        for plan in plan_resumable_batches(segments, max_chars)
    ]


def update_legacy_translation_history(
    history: dict[tuple[int, int], TranslatedSegmentEvidence],
    chapter: int,
    start_index: int,
    segments: list[Segment],
) -> None:
    """Replace the in-memory evidence index for every non-empty translated segment."""

    for offset, segment in enumerate(segments):
        target = (segment.target or "").strip()
        if not target:
            continue
        segment_index = start_index + offset
        history[(chapter, segment_index)] = TranslatedSegmentEvidence(
            chapter=chapter,
            segment=segment_index,
            source=segment.source,
            target=target,
        )


def legacy_translation_progress_counts(
    store: RunStore,
    chapter_indices: list[int],
    *,
    max_chars_per_batch: int,
    plan_batches: PlanBatchesFn = resume_legacy_batches,
) -> tuple[int, int]:
    """Count only fully durable batches so resumed progress cannot double-count."""

    total = 0
    done = 0
    for chapter_index in chapter_indices:
        segments = store.load_chapter(chapter_index).text_segments
        total += len(segments)
        for batch in plan_batches(segments, max_chars_per_batch):
            if all(segment.target and segment.target.strip() for segment in batch):
                done += len(batch)
    return total, done


def legacy_chapter_term_snapshot(
    glossary: GlossaryStore,
    text_segments: list[Segment],
    *,
    glossary_scope: str,
) -> list[GlossaryTerm]:
    """Read the latest glossary and optionally prune it to terms used by this chapter."""

    terms = glossary.all_terms()
    if glossary_scope != "chapter":
        return terms
    source_text = "\n".join(segment.source for segment in text_segments)
    hits = {term.source for term in GlossaryStore.terms_in(terms, source_text)}
    return [term for term in terms if term.source in hits]


def legacy_chapter_progress_label(title: str, index: int) -> str:
    """Prefer the book's real chapter title and retain the historical fallback."""

    normalized = (title or "").strip()
    return normalized or f"章节 {index + 1}"


def extract_legacy_batch_glossary(
    glossary: GlossaryStore,
    store: RunStore,
    chapter: int,
    start_index: int,
    batch: list[Segment],
    translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
    source_corpus: str,
    *,
    extract_and_store: ExtractChapterGlossaryFn,
) -> dict[str, int]:
    """Commit one batch's glossary changes, then append its resume checkpoint event."""

    source_text = "\n".join(segment.source for segment in batch)
    target_text = "\n".join(segment.target or "" for segment in batch)
    summary = extract_and_store(
        glossary,
        source_text,
        target_text,
        chapter,
        history=translation_history.values(),
        before=(chapter, start_index),
        source_corpus=source_corpus,
    )

    # The DB transaction must finish before the event becomes a durable resume
    # checkpoint.  A crash in between safely repeats an idempotent upsert.
    store.log_event(
        "batch_glossary_extracted",
        chapter=chapter,
        start_index=start_index,
        count=len(batch),
        summary=summary,
    )
    return summary


def process_legacy_batch(
    batch: list[Segment],
    terms: list[GlossaryTerm],
    context_text: str,
    style: str,
    book_synopsis: str = "",
    chapter_digest: str = "",
    annotation_contexts: list[list[dict[str, str]]] | None = None,
    *,
    translate_batch: TranslateBatchFn,
    polish_batch: PolishBatchFn,
    polish_enabled: FeatureSwitch,
    backtranslate_sample: SampleRateFn,
    random_sample: RandomSampleFn,
) -> BatchResult:
    """Translate, optionally polish, and deterministically collect sampling results."""

    sources = [segment.source for segment in batch]
    targets = translate_batch(
        sources,
        glossary_terms=terms,
        style=style,
        context=context_text,
        book_synopsis=book_synopsis,
        chapter_digest=chapter_digest,
        annotation_contexts=annotation_contexts,
    )

    # A malformed polish response cannot change alignment; retain translations
    # unless the polisher returns exactly one item for each source segment.
    if polish_enabled():
        polished = polish_batch(targets, glossary_terms=terms, style=style)
        if len(polished) == len(targets):
            targets = polished

    # Sampling remains after translation/polish and before any durable write,
    # matching the old random-call and backtranslation-pair order exactly.
    samples: list[tuple[str, str]] = []
    sample_rate = backtranslate_sample()
    if sample_rate > 0:
        for source, target in zip(sources, targets):
            if random_sample() < sample_rate:
                samples.append((source, target or ""))

    return BatchResult(targets=targets, bt_samples=samples)


def translate_legacy_chapter(
    chapter_index: int,
    store: RunStore,
    glossary: GlossaryStore,
    context: RollingContext,
    style: str,
    book_synopsis: str = "",
    *,
    policy: TranslationPolicy,
    translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
    source_corpus: str,
    annotation_context_registry: dict[str, Any] | None,
    process_batch: ProcessBatchFn,
    term_snapshot: TermSnapshotFn,
    extract_batch_glossary: ExtractBatchGlossaryFn,
    align_after_batch: AlignAfterBatchFn,
    sync_context_chapter_prefix: SyncContextFn,
    update_translation_history: UpdateHistoryFn,
    annotation_contexts_for_segments: AnnotationContextsFn,
    chapter_progress_label: ChapterLabelFn,
    extract_chapter_glossary: ExtractChapterGlossaryFn,
    backtranslation_check: BacktranslationCheckFn,
    polish_enabled: FeatureSwitch,
    punctuation_enabled: FeatureSwitch,
    rolling_context_segments: ContextWindowFn,
    plan_batches: PlanBatchesFn = resume_legacy_batches,
    report_progress: ReportProgressFn = report_translation_progress,
    progress: ProgressFn | None = None,
    done: int = 0,
    total: int = 0,
) -> int:
    """Run one legacy chapter while preserving every old crash/resume boundary."""

    chapter = store.load_chapter(chapter_index)
    text_segments = chapter.text_segments
    if not text_segments:
        # Empty chapters have no body artifact to publish; status is durable
        # before the observability event, exactly as in the old implementation.
        store.set_chapter_status(chapter_index, STATUS_DONE)
        store.log_event("chapter_skipped", chapter=chapter_index, reason="empty")
        return done

    chapter_digest = chapter.meta.get("source_digest", "")
    annotation_contexts = annotation_contexts_for_segments(
        text_segments,
        annotation_context_registry,
    )
    batches = plan_batches(text_segments, policy.max_chars_per_batch)
    chapter_done = sum(
        len(batch)
        for batch in batches
        if all(segment.target and segment.target.strip() for segment in batch)
    )
    label = chapter_progress_label(chapter.title, chapter_index)

    # Refresh the label before any resumed glossary model call so the UI cannot
    # continue displaying the previous preparation-stage label.
    report_progress(
        progress,
        chapter_done=chapter_done,
        chapter_total=len(text_segments),
        overall_done=done,
        overall_total=total,
        label=label,
    )
    glossary_checkpoints = store.completed_batch_glossary_keys(chapter_index)
    current_terms = term_snapshot(glossary, text_segments)

    # Batches are deliberately serial: each durable translation/glossary update
    # becomes input to the next model request in this chapter.
    backtranslation_samples: list[tuple[str, str]] = []
    segment_base = 0
    for batch in batches:
        batch_start = segment_base
        glossary_key = store.batch_glossary_key(batch_start, len(batch))
        existing_targets = [
            segment.target for segment in batch if segment.target and segment.target.strip()
        ]

        if len(existing_targets) == len(batch):
            # Resume path: rebuild annotation/context state first.  Glossary is
            # repeated only when no durable extraction event exists.
            align_after_batch(
                chapter_index,
                chapter,
                batch_start,
                len(batch),
                store,
            )
            context.add_targets([segment.target or "" for segment in batch])
            sync_context_chapter_prefix(
                context,
                text_segments,
                batch_start + len(batch),
            )
            if glossary_key in glossary_checkpoints:
                glossary_summary = {
                    "inserted": 0,
                    "conflict": 0,
                    "unchanged": 0,
                    "updated": 0,
                    "skipped": 1,
                }
            else:
                glossary_summary = extract_batch_glossary(
                    glossary,
                    store,
                    chapter_index,
                    batch_start,
                    batch,
                    translation_history,
                    source_corpus,
                )
                glossary_checkpoints.add(glossary_key)
            current_terms = term_snapshot(glossary, text_segments)
            store.log_event(
                "batch_skipped",
                chapter=chapter_index,
                start_index=batch_start,
                count=len(batch),
                reason="already_translated",
                glossary_extraction=glossary_summary,
                segments=[
                    {
                        "index": segment_base + offset,
                        "source": segment.source,
                        "target": segment.target,
                    }
                    for offset, segment in enumerate(batch)
                ],
            )
            segment_base += len(batch)
            report_progress(
                progress,
                chapter_done=chapter_done,
                chapter_total=len(text_segments),
                overall_done=done,
                overall_total=total,
                label=label,
            )
            continue

        context_text = context.render(rolling_context_segments())
        result = process_batch(
            batch,
            current_terms,
            context_text,
            style,
            book_synopsis,
            chapter_digest,
            annotation_contexts=annotation_contexts[batch_start : batch_start + len(batch)],
        )
        for segment, target in zip(batch, result.targets):
            segment.target = target
        backtranslation_samples.extend(result.bt_samples)

        # This is the primary resume boundary: targets must be durable before
        # annotations, context, events, counters, or glossary can advance.
        store.save_chapter(chapter)
        align_after_batch(
            chapter_index,
            chapter,
            batch_start,
            len(batch),
            store,
        )
        context.add_targets([segment.target or "" for segment in batch])
        sync_context_chapter_prefix(
            context,
            text_segments,
            batch_start + len(batch),
        )
        store.log_event(
            "batch_translated",
            chapter=chapter_index,
            start_index=batch_start,
            count=len(batch),
            polished=polish_enabled(),
            punctuation_normalized=punctuation_enabled(),
            backtranslate_sample_count=len(result.bt_samples),
            segments=[
                {
                    "index": batch_start + offset,
                    "source": segment.source,
                    "target": segment.target,
                }
                for offset, segment in enumerate(batch)
            ],
        )
        done += len(batch)
        chapter_done += len(batch)
        segment_base += len(batch)
        report_progress(
            progress,
            chapter_done=chapter_done,
            chapter_total=len(text_segments),
            overall_done=done,
            overall_total=total,
            label=label,
        )

        # Glossary must never lead the saved chapter.  Its event is the resume
        # checkpoint, followed by in-memory evidence and prompt snapshot refresh.
        extract_batch_glossary(
            glossary,
            store,
            chapter_index,
            batch_start,
            batch,
            translation_history,
            source_corpus,
        )
        update_translation_history(
            translation_history,
            chapter_index,
            batch_start,
            batch,
        )
        glossary_checkpoints.add(glossary_key)
        current_terms = term_snapshot(glossary, text_segments)

    # Chapter-level punctuation is applied after every batch so paired marks can
    # span physical segments.  Update retained context and evidence immediately.
    if punctuation_enabled():
        translated = [segment.target or "" for segment in text_segments]
        normalized_targets = normalize_zh_segments(
            translated,
            [segment.cont for segment in text_segments],
        )
        for segment, normalized in zip(text_segments, normalized_targets):
            segment.target = normalized
        retained = min(len(normalized_targets), len(context.recent_targets))
        if retained:
            context.recent_targets[-retained:] = normalized_targets[-retained:]
        update_translation_history(
            translation_history,
            chapter_index,
            0,
            text_segments,
        )

    # The chapter-wide glossary pass catches cross-segment terms.  Commit it and
    # append its event before optional backtranslation and final publication.
    source_text = "\n".join(segment.source for segment in text_segments)
    target_text = "\n".join(segment.target or "" for segment in text_segments)
    chapter_glossary_summary = extract_chapter_glossary(
        glossary,
        source_text,
        target_text,
        chapter_index,
        history=translation_history.values(),
        before=(chapter_index, len(text_segments)),
        source_corpus=source_corpus,
    )
    store.log_event(
        "chapter_glossary_extracted",
        chapter=chapter_index,
        summary=chapter_glossary_summary,
    )

    backtranslation_issues: list[dict[str, Any]] = []
    if backtranslation_samples:
        sources = [source for source, _target in backtranslation_samples]
        targets = [target for _source, target in backtranslation_samples]
        for issue in backtranslation_check(sources, targets):
            issue["chapter"] = chapter_index
            backtranslation_issues.append(issue)
        store.log_event(
            "chapter_backtranslation_checked",
            chapter=chapter_index,
            sample_count=len(backtranslation_samples),
            issue_count=len(backtranslation_issues),
            issues=backtranslation_issues,
        )

    # Content and manifest status share one state lock.  The completion event is
    # intentionally last so observers never see "done" before the durable state.
    chapter.meta["backtranslation_issues"] = backtranslation_issues
    store.save_chapter_with_status(chapter, STATUS_DONE)
    store.log_event(
        "chapter_done",
        chapter=chapter_index,
        title=chapter.title,
        segment_count=len(text_segments),
        backtranslation_issue_count=len(backtranslation_issues),
    )
    return done


__all__ = [
    "BatchResult",
    "TranslationPolicy",
    "extract_legacy_batch_glossary",
    "legacy_chapter_progress_label",
    "legacy_chapter_term_snapshot",
    "legacy_translation_progress_counts",
    "process_legacy_batch",
    "report_translation_progress",
    "resume_legacy_batches",
    "translate_legacy_chapter",
    "update_legacy_translation_history",
]
