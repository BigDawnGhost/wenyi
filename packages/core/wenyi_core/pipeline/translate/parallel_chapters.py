"""Concurrent chapter translation executor with a single ordered state writer.

Worker threads perform only model calls (translate + optional polish) over detached
chapter specs with a frozen glossary snapshot and deterministic positional source
lookbehind. The calling thread alone reads/writes Storage, aligns annotations/styles,
flushes usage, and publishes ``STATUS_DONE`` in deterministic chapter order.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from ...events import ProgressFn
from ...llm.retrying import is_resumable_provider_interrupt
from ...markup.contexts import annotation_contexts_for_segments
from ...models.document import Chapter, Segment
from ...models.glossary import GlossaryTerm
from ...storage.protocol import Storage
from ...storage.run_store import STATUS_DONE
from .babeldoc_context import BabeldocContext
from .context import following_chapter_source, render_recent_translations
from .post_normalize import repair_escaped_newlines
from .translation_batch import (
    POLISH_ATTEMPTS,
    POLISH_FAILED,
    POLISH_PENDING,
    BatchDraft,
    BatchPlan,
    BatchResult,
    PolishJob,
    TranslationBatchExecutor,
    keep_nonempty_drafts,
    mark_polish_failure,
)


@dataclass(frozen=True)
class _ChapterBatchSpec:
    batch_start: int
    segment_indices: tuple[int, ...]
    sources: tuple[str, ...]
    annotation_contexts: list[list[dict[str, str]]]
    next_source: str
    body_context: bool
    already_done: bool
    existing_targets: tuple[str | None, ...]
    existing_before_polish: tuple[str | None, ...] = ()
    needs_repolish: bool = False
    repolish_eligible: tuple[bool, ...] = ()


@dataclass(frozen=True)
class _ChapterSpec:
    chapter: int
    title: str
    chapter_digest: str
    opening_context: str
    style: str
    book_synopsis: str
    terms: tuple[GlossaryTerm, ...]
    rolling_context_tokens: int
    batches: tuple[_ChapterBatchSpec, ...]


@dataclass(frozen=True)
class _BatchOutcome:
    batch_start: int
    sources: tuple[str, ...]
    result: BatchResult
    repolish_only: bool = False
    polish_error_reason: str | None = None


@dataclass(frozen=True)
class _ChapterOutcome:
    chapter: int
    batches: tuple[_BatchOutcome, ...]
    error: BaseException | None
    completed: bool


def _stops_run(exc: BaseException) -> bool:
    return not isinstance(exc, Exception) or is_resumable_provider_interrupt(exc)


def _segment_repolish_eligible(seg: Segment) -> bool:
    """Return True when a segment is marked for repolish and has not been manually edited."""
    if not (seg.meta.get(POLISH_PENDING) or seg.meta.get(POLISH_FAILED)):
        return False
    if seg.target_before_polish is None:
        return True
    return seg.target == repair_escaped_newlines(seg.source, seg.target_before_polish)


def pending_or_unpolished_chapters(
    store: Storage, manifest: dict[str, Any], *, include_unpolished: bool
) -> list[int]:
    """Return pending chapters plus any ``done`` chapters carrying ``unpolished=True``."""
    targets = store.pending_chapters()
    if not include_unpolished:
        return targets
    unpolished = {
        row["index"]
        for row in manifest.get("chapters", [])
        if row.get("unpolished") and row["index"] not in targets
    }
    if not unpolished:
        return targets
    needed = set(targets) | unpolished
    return [row["index"] for row in manifest.get("chapters", []) if row["index"] in needed]


def publish_chapter_status(store: Storage, chapter: Chapter, *, polish: bool) -> None:
    """Persist ``STATUS_DONE`` and synchronize the chapter/manifest ``unpolished`` retry flag."""
    ci = chapter.index
    text_segs = chapter.text_segments
    if not polish:
        for seg in text_segs:
            seg.meta.pop(POLISH_PENDING, None)
            seg.meta.pop(POLISH_FAILED, None)
            seg.meta.pop(POLISH_ATTEMPTS, None)
    unpolished = polish and any(
        seg.meta.get(POLISH_FAILED) or seg.meta.get(POLISH_PENDING) for seg in text_segs
    )
    if unpolished:
        chapter.meta["unpolished"] = True
        # Write the manifest retry marker before marking the chapter done so an interrupt
        # between the two writes never leaves an unpolished chapter silently skipped.
        with store.state_lock():
            manifest = store.load_manifest()
            for row in manifest.get("chapters", []):
                if row.get("index") == ci and not row.get("unpolished"):
                    row["unpolished"] = True
                    store.save_manifest(manifest)
                    break
    else:
        chapter.meta.pop("unpolished", None)
    store.save_chapter_with_status(chapter, STATUS_DONE)
    if not unpolished:
        with store.state_lock():
            manifest = store.load_manifest()
            for row in manifest.get("chapters", []):
                if row.get("index") == ci and row.get("unpolished"):
                    row.pop("unpolished", None)
                    store.save_manifest(manifest)
                    break
    store.log_event(
        "chapter_done",
        chapter=ci,
        title=chapter.title,
        segment_count=len(text_segs),
        **({"unpolished": True} if unpolished else {}),
    )


def _source_lookbehind(prev_chapter: Chapter | None, budget_tokens: int) -> str:
    """Build deterministic opening context from the preceding chapter's raw source tail."""
    if prev_chapter is None or budget_tokens <= 0:
        return ""
    parts = [seg.source for seg in prev_chapter.text_segments if seg.source.strip()]
    if not parts:
        return ""
    tail = render_recent_translations(reversed(parts), budget_tokens)
    if not tail:
        return ""
    return f"[Preceding chapter source tail (reference only)]\n{tail}"


class ParallelChapterRunner:
    """Translate pending chapters concurrently with bounded workers and one ordered writer."""

    def __init__(
        self,
        batches: TranslationBatchExecutor,
        *,
        chapter_batches_fn: Callable[[Chapter], list[list[Segment]]],
        align_batch_fn: Callable[[Storage, Chapter, list[Segment], int, int], None],
        progress_label_fn: Callable[[str, int], str],
        flush_usage_fn: Callable[[Storage, str], None],
    ):
        self._batches = batches
        self._chapter_batches = chapter_batches_fn
        self._align_batch = align_batch_fn
        self._progress_label = progress_label_fn
        self._flush_usage = flush_usage_fn

    def run(
        self,
        targets: list[int],
        store: Storage,
        *,
        frozen_terms: list[GlossaryTerm],
        style: str,
        book_synopsis: str,
        annotation_context_registry: dict[str, Any] | None,
        chapter_concurrency: int,
        rolling_context_tokens: int,
        polish: bool,
        polish_mode: str,
        progress: ProgressFn | None,
        done: int,
        total: int,
    ) -> int:
        """Translate ``targets`` concurrently, applying and persisting each chapter on this thread."""
        manifest = store.load_manifest()
        all_indices = sorted(
            row["index"]
            for row in manifest.get("chapters", [])
            if isinstance(row.get("index"), int)
        )
        loaded: dict[int, Chapter] = {ci: store.load_chapter(ci) for ci in all_indices}

        specs: list[_ChapterSpec] = []
        chapters_by_id: dict[int, Chapter] = {}
        align_existing: dict[int, list[tuple[int, int]]] = {}

        for ci in targets:
            chapter = loaded[ci]
            text_segs = chapter.text_segments
            if not text_segs:
                store.set_chapter_status(ci, STATUS_DONE)
                store.log_event("chapter_skipped", chapter=ci, reason="empty")
                continue
            chapters_by_id[ci] = chapter
            digest = str(chapter.meta.get("source_digest") or "")
            policy = BabeldocContext.for_chapter(chapter)
            ann_contexts = annotation_contexts_for_segments(text_segs, annotation_context_registry)
            batches = self._chapter_batches(chapter)

            prev_indices = [idx for idx in all_indices if idx < ci]
            prev_chapter = loaded[prev_indices[-1]] if prev_indices else None
            opening_context = _source_lookbehind(prev_chapter, rolling_context_tokens)

            batch_specs: list[_ChapterBatchSpec] = []
            cleared_stale_polish = False
            seg_base = 0
            for b in batches:
                end = seg_base + len(b)
                body_ctx = policy is None or policy.body[seg_base]
                already_done = all(s.target is not None for s in b)
                marked = already_done and any(
                    s.meta.get(POLISH_PENDING) or s.meta.get(POLISH_FAILED) for s in b
                )
                needs_repolish = marked and polish
                repolish_eligible = (
                    tuple(_segment_repolish_eligible(s) for s in b) if needs_repolish else ()
                )
                if marked and not polish:
                    for s in b:
                        s.meta.pop(POLISH_PENDING, None)
                        s.meta.pop(POLISH_FAILED, None)
                        s.meta.pop(POLISH_ATTEMPTS, None)
                    cleared_stale_polish = True
                if already_done:
                    align_existing.setdefault(ci, []).append((seg_base, len(b)))
                next_src = following_chapter_source(store, chapter, end, policy) if body_ctx else ""
                batch_specs.append(
                    _ChapterBatchSpec(
                        batch_start=seg_base,
                        segment_indices=tuple(s.index for s in b),
                        sources=tuple(s.source for s in b),
                        annotation_contexts=ann_contexts[seg_base:end],
                        next_source=next_src,
                        body_context=body_ctx,
                        already_done=already_done,
                        existing_targets=tuple(s.target for s in b),
                        existing_before_polish=tuple(s.target_before_polish for s in b),
                        needs_repolish=needs_repolish,
                        repolish_eligible=repolish_eligible,
                    )
                )
                seg_base = end
            if cleared_stale_polish:
                store.save_chapter(chapter)

            specs.append(
                _ChapterSpec(
                    chapter=ci,
                    title=chapter.title,
                    chapter_digest=digest,
                    opening_context=opening_context,
                    style=style,
                    book_synopsis=book_synopsis,
                    terms=tuple(frozen_terms),
                    rolling_context_tokens=rolling_context_tokens,
                    batches=tuple(batch_specs),
                )
            )

        if not specs:
            return done

        # Warm the DeepSeek KV prefix with the first pending chapter's first batch implicitly
        # by submitting bounded waves, keeping at most chapter_concurrency chapters in flight.
        workers = min(max(1, chapter_concurrency), len(specs))
        stop = threading.Event()
        next_pos = 0
        in_flight: dict[Future[_ChapterOutcome], int] = {}
        first_error: BaseException | None = None

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="wenyi-chapter-worker"
        ) as executor:
            applied_chapters: set[int] = set()
            try:
                while (next_pos < len(specs) and not stop.is_set()) or in_flight:
                    while not stop.is_set() and next_pos < len(specs) and len(in_flight) < workers:
                        spec = specs[next_pos]
                        ctx = contextvars.copy_context()
                        fut = executor.submit(
                            ctx.run,
                            self._run_chapter_worker,
                            spec,
                            polish,
                            polish_mode,
                            stop,
                        )
                        in_flight[fut] = spec.chapter
                        next_pos += 1
                    if not in_flight:
                        break
                    finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    # Keep every future tracked in ``in_flight`` until its chapter is durably
                    # saved so an interrupt at any handoff point still recovers it cleanly.
                    ready_pairs = [(fut, fut.result()) for fut in finished]
                    ready_pairs.sort(key=lambda pair: pair[1].chapter)

                    # Phase 1: apply once in memory and durably save every completed chapter in
                    # the wave BEFORE removing its future, emitting events, or aligning.
                    last_label: str | None = None
                    to_finalize: list[tuple[_ChapterOutcome, list[tuple[int, int]]]] = []
                    pending_events: list[tuple[str, dict[str, Any]]] = []
                    for fut, outcome in ready_pairs:
                        chapter = chapters_by_id[outcome.chapter]
                        align_ranges: list[tuple[int, int]] = []
                        events: list[tuple[str, dict[str, Any]]] = []
                        if outcome.chapter not in applied_chapters:
                            done, align_ranges, events = self._apply_outcome_to_chapter(
                                chapter,
                                outcome,
                                polish=polish,
                                done=done,
                            )
                            applied_chapters.add(outcome.chapter)
                        if outcome.batches:
                            store.save_chapter(chapter)
                        in_flight.pop(fut, None)
                        pending_events.extend(events)
                        last_label = self._progress_label(chapter.title, outcome.chapter)
                        if outcome.error is not None:
                            stop.set()
                            if first_error is None:
                                first_error = outcome.error
                        else:
                            to_finalize.append((outcome, align_ranges))
                    self._flush_usage(store, "chapter")
                    for event_name, payload in pending_events:
                        store.log_event(event_name, **payload)

                    # Phase 2: run alignment and chapter publication only after all wave
                    # targets are durably saved. Alignment remains resumable if cancelled.
                    for outcome, align_ranges in to_finalize:
                        self._finalize_outcome(
                            store,
                            chapters_by_id[outcome.chapter],
                            outcome,
                            align_existing.get(outcome.chapter, ()),
                            align_ranges,
                            polish=polish,
                        )
                    if to_finalize:
                        self._flush_usage(store, "chapter")

                    if (
                        progress is not None
                        and last_label is not None
                        and first_error is None
                        and not stop.is_set()
                    ):
                        progress(done, total, last_label)
            except BaseException:
                # Signal workers to stop, cancel unstarted futures, and durably save every
                # already-done or running future without reapplying already-mutated chapters.
                stop.set()
                remaining_futs: list[Future[_ChapterOutcome]] = []
                for fut in list(in_flight):
                    if fut.done() or not fut.cancel():
                        remaining_futs.append(fut)
                in_flight.clear()
                drained: list[_ChapterOutcome] = []
                for fut in remaining_futs:
                    try:
                        drained.append(fut.result())
                    except BaseException:  # noqa: BLE001 - best-effort drain on interruption.
                        pass
                drained.sort(key=lambda item: item.chapter)
                for outcome in drained:
                    chapter = chapters_by_id[outcome.chapter]
                    if outcome.chapter not in applied_chapters:
                        done, _, _ = self._apply_outcome_to_chapter(
                            chapter,
                            outcome,
                            polish=polish,
                            done=done,
                        )
                        applied_chapters.add(outcome.chapter)
                    if outcome.batches:
                        store.save_chapter(chapter)
                if drained:
                    self._flush_usage(store, "chapter")
                raise

        if first_error is not None:
            raise first_error
        return done

    def _run_chapter_worker(
        self,
        spec: _ChapterSpec,
        polish: bool,
        polish_mode: str,
        stop: threading.Event,
    ) -> _ChapterOutcome:
        """Execute one chapter's pending batches serially in a worker thread (model calls only)."""
        wave_drafts: list[str] = []
        outcomes: list[_BatchOutcome] = []
        error: BaseException | None = None
        stopped_early = False

        for bspec in spec.batches:
            if bspec.already_done:
                if not bspec.needs_repolish:
                    if bspec.body_context:
                        wave_drafts.extend(t or "" for t in bspec.existing_targets)
                    continue
                if stop.is_set():
                    stopped_early = True
                    break
                draft_targets = tuple(
                    bp if bp is not None else (tgt or "")
                    for bp, tgt in zip(bspec.existing_before_polish, bspec.existing_targets)
                )
                job = PolishJob(
                    spec.chapter,
                    bspec.batch_start,
                    bspec.segment_indices,
                    bspec.sources,
                    spec.terms,
                    spec.style,
                    bspec.next_source,
                    BatchDraft(draft_targets),
                )
                polish_error_reason: str | None = None
                try:
                    raw_polished = self._batches.polish(job, polish_mode=polish_mode)
                except BaseException as exc:  # noqa: BLE001 - preserve prior batches on stop.
                    if _stops_run(exc):
                        error = exc
                        break
                    raw_polished = None
                    polish_error_reason = f"{type(exc).__name__}: {exc}"

                if raw_polished is not None and len(raw_polished) == len(draft_targets):
                    kept_targets, empty_reverted = keep_nonempty_drafts(draft_targets, raw_polished)
                    cleaned = tuple(
                        repair_escaped_newlines(src, tgt)
                        for src, tgt in zip(bspec.sources, kept_targets)
                    )
                    repolish_res = BatchResult(
                        cleaned,
                        bspec.existing_before_polish,
                        polish_failed=False,
                        polish_empty_reverted=empty_reverted,
                    )
                else:
                    repolish_res = BatchResult(
                        tuple(t or "" for t in bspec.existing_targets),
                        bspec.existing_before_polish,
                        polish_failed=True,
                    )
                outcomes.append(
                    _BatchOutcome(
                        bspec.batch_start,
                        bspec.sources,
                        repolish_res,
                        repolish_only=True,
                        polish_error_reason=polish_error_reason,
                    )
                )
                if bspec.body_context:
                    # Feed rolling context with the exact targets the writer will accept,
                    # keeping saved/manually-edited targets for ineligible segments.
                    if repolish_res.polish_failed:
                        wave_drafts.extend(t or "" for t in bspec.existing_targets)
                    else:
                        eligible_flags = bspec.repolish_eligible or (
                            (True,) * len(repolish_res.targets)
                        )
                        wave_drafts.extend(
                            polished_tgt if ok else (saved_tgt or "")
                            for polished_tgt, saved_tgt, ok in zip(
                                repolish_res.targets, bspec.existing_targets, eligible_flags
                            )
                        )
                continue

            if stop.is_set():
                stopped_early = True
                break

            if not bspec.body_context:
                ctx_text = ""
            elif wave_drafts:
                ctx_text = render_recent_translations(
                    reversed(wave_drafts), spec.rolling_context_tokens
                )
            else:
                ctx_text = spec.opening_context

            plan = BatchPlan(
                chapter=spec.chapter,
                start_index=bspec.batch_start,
                segment_indices=bspec.segment_indices,
                sources=bspec.sources,
                terms=spec.terms,
                context=ctx_text,
                style=spec.style,
                book_synopsis=spec.book_synopsis if bspec.body_context else "",
                chapter_digest=spec.chapter_digest if bspec.body_context else "",
                annotation_contexts=bspec.annotation_contexts,
                next_source=bspec.next_source,
                allow_empty_translations=False,
            )
            try:
                result = self._batches.execute(plan, polish=polish, polish_mode=polish_mode)
            except BaseException as exc:  # noqa: BLE001 - save completed batches before propagating.
                error = exc
                break

            cleaned = tuple(
                repair_escaped_newlines(src, tgt) for src, tgt in zip(bspec.sources, result.targets)
            )
            cleaned_before = tuple(
                repair_escaped_newlines(src, bp) if bp is not None else None
                for src, bp in zip(bspec.sources, result.before_polish)
            )
            result = BatchResult(
                cleaned,
                cleaned_before,
                precision_key=result.precision_key,
                polish_failed=result.polish_failed,
                polish_empty_reverted=result.polish_empty_reverted,
                polish_interrupted=result.polish_interrupted,
            )
            outcomes.append(_BatchOutcome(bspec.batch_start, bspec.sources, result))
            if bspec.body_context:
                wave_drafts.extend(cleaned)
            if result.polish_interrupted is not None and _stops_run(result.polish_interrupted):
                error = result.polish_interrupted
                break

        completed = error is None and not stopped_early
        return _ChapterOutcome(spec.chapter, tuple(outcomes), error, completed)

    def _apply_outcome_to_chapter(
        self,
        chapter: Chapter,
        outcome: _ChapterOutcome,
        *,
        polish: bool,
        done: int,
    ) -> tuple[int, list[tuple[int, int]], list[tuple[str, dict[str, Any]]]]:
        """Apply every completed batch in ``outcome`` onto ``chapter`` once in memory."""
        ci = outcome.chapter
        text_segs = chapter.text_segments
        align_ranges: list[tuple[int, int]] = []
        events: list[tuple[str, dict[str, Any]]] = []

        for item in outcome.batches:
            count = len(item.sources)
            batch_segs = text_segs[item.batch_start : item.batch_start + count]
            if item.repolish_only:
                applied = False
                for seg, target in zip(batch_segs, item.result.targets):
                    eligible = bool(seg.meta.pop(POLISH_PENDING, False)) or bool(
                        seg.meta.get(POLISH_FAILED, False)
                    )
                    if item.result.polish_failed:
                        if eligible:
                            mark_polish_failure(seg, interrupted=False)
                        continue
                    if not eligible:
                        continue
                    norm_bp = (
                        repair_escaped_newlines(seg.source, seg.target_before_polish)
                        if seg.target_before_polish is not None
                        else None
                    )
                    if norm_bp is not None and seg.target != norm_bp:
                        seg.meta.pop(POLISH_FAILED, None)
                        seg.meta.pop(POLISH_ATTEMPTS, None)
                        continue
                    seg.target = target
                    seg.meta.pop(POLISH_FAILED, None)
                    seg.meta.pop(POLISH_ATTEMPTS, None)
                    applied = True
                if item.result.polish_empty_reverted:
                    events.append(
                        (
                            "batch_polish_empty_kept",
                            {
                                "chapter": ci,
                                "start_index": item.batch_start,
                                "count": count,
                                "reverted_indices": list(item.result.polish_empty_reverted),
                            },
                        )
                    )
                if applied or not item.result.polish_failed:
                    if applied:
                        align_ranges.append((item.batch_start, count))
                    events.append(
                        (
                            "batch_polished",
                            {"chapter": ci, "start_index": item.batch_start, "count": count},
                        )
                    )
                else:
                    events.append(
                        (
                            "batch_polish_failed",
                            {
                                "chapter": ci,
                                "start_index": item.batch_start,
                                "count": count,
                                "reason": item.polish_error_reason or "misaligned_output",
                            },
                        )
                    )
                continue

            for seg, target, before_polish in zip(
                batch_segs, item.result.targets, item.result.before_polish
            ):
                seg.target = target
                seg.target_before_polish = before_polish
                seg.meta.pop(POLISH_PENDING, None)
                if polish and item.result.polish_failed:
                    mark_polish_failure(seg, interrupted=item.result.polish_interrupted is not None)
                else:
                    seg.meta.pop(POLISH_FAILED, None)
                    seg.meta.pop(POLISH_ATTEMPTS, None)
            align_ranges.append((item.batch_start, count))
            if item.result.polish_empty_reverted:
                events.append(
                    (
                        "batch_polish_empty_kept",
                        {
                            "chapter": ci,
                            "start_index": item.batch_start,
                            "count": count,
                            "reverted_indices": list(item.result.polish_empty_reverted),
                        },
                    )
                )
            if item.result.polish_interrupted is not None:
                events.append(
                    (
                        "batch_polish_failed",
                        {
                            "chapter": ci,
                            "start_index": item.batch_start,
                            "count": count,
                            "reason": "provider_interrupted",
                        },
                    )
                )
            elif item.result.polish_failed:
                events.append(
                    (
                        "batch_polish_failed",
                        {
                            "chapter": ci,
                            "start_index": item.batch_start,
                            "count": count,
                            "reason": "misaligned_output",
                        },
                    )
                )
            events.append(
                (
                    "batch_translated",
                    {
                        "chapter": ci,
                        "start_index": item.batch_start,
                        "count": count,
                        "polished": polish and not item.result.polish_failed,
                        "translation_mode": "standard",
                        "segments": [
                            {"index": item.batch_start + i, "source": s.source, "target": s.target}
                            for i, s in enumerate(batch_segs)
                        ],
                    },
                )
            )
            done += count

        return done, align_ranges, events

    def _finalize_outcome(
        self,
        store: Storage,
        chapter: Chapter,
        outcome: _ChapterOutcome,
        existing_batches: list[tuple[int, int]] | tuple[tuple[int, int], ...],
        new_align_ranges: list[tuple[int, int]],
        *,
        polish: bool,
    ) -> None:
        """Run resumable annotation/style alignment and publish ``STATUS_DONE`` if complete."""
        text_segs = chapter.text_segments
        for start, count in existing_batches:
            self._align_batch(store, chapter, text_segs, start, count)
        for start, count in new_align_ranges:
            self._align_batch(store, chapter, text_segs, start, count)
        if outcome.completed:
            publish_chapter_status(store, chapter, polish=polish)
