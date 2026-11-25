"""Chapter-level Review scanning, adaptive recovery and stable merging."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from threading import Lock
from types import SimpleNamespace
from typing import Any

from ...agents.review_loop import ReviewAgentLoop
from ...agents.reviewer import Reviewer, ReviewOutputError
from ...config import Config
from ...ingest.tokens import count_tokens
from ...llm.base import LLMClient
from ...models.glossary import GlossaryTerm
from ...review.evidence import BookEvidenceIndex
from ...review.models import integer_index
from ...review.run_store import ReviewRunStore
from .review_checkpoint import ReviewTraceStore

# Events a chunk emits while producing its result, ordered relative to each other.
# A chunk's own completion comes last: it summarises what happened before it.
_EVENT_ORDER = {
    "review_json_repaired": 0,
    "review_chunk_split": 0,
    "review_singleton_retry": 1,
    "review_singleton_recovered": 2,
    "review_singleton_failed": 2,
    "review_leaf_finished": 3,
}


def order_buffered_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order buffered review events by position in the book, not by arrival.

    Workers finish in whatever order the pool schedules them. Writing straight
    from a worker would put the log in completion order, and resume plus audit
    both read that log as if it followed the source.
    """
    return sorted(
        events,
        key=lambda row: (
            row.get("start_index", row.get("chunk_base", -1)),
            -row.get("count", row.get("segment_count", 0)),
            _EVENT_ORDER.get(row.get("event", ""), 99),
            row.get("attempt", 0),
        ),
    )


class ReviewChunkService:
    """Execute fixed chapter snapshots without owning session policy or book state."""

    def __init__(self, config: Config, client: LLMClient, reviewer: Reviewer):
        self._config = config
        self._client = client
        self._reviewer = reviewer

    def _review_chunk_adaptive(
        self,
        chunk_base: int,
        chunk: list,
        terms: list[GlossaryTerm],
        *,
        chapter_index: int | None,
        evidence: BookEvidenceIndex | None,
        debug: ReviewRunStore | None,
        target_overrides: Mapping[tuple[int, int], str] | None,
        review_round: int | None,
        buffer_event: Callable[..., None],
    ) -> list[dict]:
        """Run one contiguous review block with adaptive bisection and bounded singleton retries."""

        def review_once(base: int, segs: list, *, attempt: int = 1) -> list[dict]:
            """Run one review call and map valid block-local indices to chapter indices."""
            srcs = [s.source for s in segs]
            overrides = target_overrides or {}

            def target_for(local_index: int, segment) -> str:
                """Read this round's shadow target, falling back to formal text when chapter
                position is unavailable.
                """
                if chapter_index is None:
                    return segment.target or ""
                return overrides.get(
                    (chapter_index, base + local_index),
                    segment.target or "",
                )

            tgts = [target_for(local_index, segment) for local_index, segment in enumerate(segs)]

            # Check the chunk cache to skip reviewer and evidence-loop model calls on resume.
            round_prefix = f"r{review_round}-" if review_round is not None else ""
            chunk_id = f"{round_prefix}ch{chapter_index}-base{base}-n{len(segs)}"
            if debug is not None:
                cached = debug.load_chunk_result(chunk_id)
                if cached is not None:
                    # Restore initial/dismissed aggregation needed by the report.
                    if chapter_index is not None:
                        debug.record_initial_issues(
                            chapter=chapter_index,
                            chunk_base=base,
                            issues=cached.get("initial_issues", []),
                        )
                        debug.record_dismissed(
                            chapter=chapter_index,
                            chunk_base=base,
                            issues=cached.get("dismissed", []),
                        )
                    return cached.get("issues", [])

            # Probe child chunk caches using boundaries compatible with adaptive recovery.
            # Match review_adaptive's recursive bisection. If every child is cached,
            # merge them directly without making a reviewer call.
            if debug is not None and len(segs) > 1:
                cached_sub = self.try_cached_subchunks(
                    base,
                    segs,
                    debug,
                    round_prefix,
                    chapter_index,
                )
                if cached_sub is not None:
                    return cached_sub

            local_issues: list[dict] = []
            initial_trace: dict[str, Any] | None = None
            initial_path = ""
            initial_issue_count = 0
            repaired = False
            reused_initial: dict[str, Any] | None = None
            if debug is not None:
                round_prefix = f"r{review_round}-" if review_round is not None else ""
                initial_id = (
                    f"initial-{round_prefix}ch{chapter_index}-base{base}"
                    f"-n{len(segs)}-attempt{attempt}"
                )
                initial_path = f"initial/{initial_id}.json"
                # Reuse completed initial screening and skip its expensive reviewer call.
                # The initial run already validated block-local indices.
                existing_initial = debug.load_json(initial_path)
                if (
                    existing_initial is not None
                    and existing_initial.get("status") == "finished"
                    and isinstance(existing_initial.get("issues"), list)
                ):
                    reused_initial = existing_initial
                else:
                    if (
                        len(segs) > 1
                        and existing_initial
                        and existing_initial.get("status") == "failed"
                    ):
                        failure = existing_initial.get("error")
                        # Older runs stored the provider error type directly.
                        if isinstance(failure, dict) and (
                            failure.get("reason") == "truncated_response"
                            or failure.get("type")
                            in ("TruncatedResponseError", "_SafeTruncatedResponseError")
                        ):
                            raise ReviewOutputError("truncated_response")
                    initial_trace = {
                        "agent_id": initial_id,
                        "chapter": chapter_index,
                        "chunk_base": base,
                        "segment_count": len(segs),
                        "attempt": attempt,
                        "status": "running",
                    }
                    debug.write_json(initial_path, initial_trace)

            if reused_initial is not None:
                # Reuse intentionally handles invalid indices differently from fresh output: discard them
                # instead of raising ReviewOutputError. The original run already validated these results,
                # so invalid cached indices indicate edited or stale trace data. Avoid turning the whole chunk
                # into fallback during recovery. Session-level content fingerprints guard against
                # ordinary source-content changes.
                local_issues = [dict(issue) for issue in reused_initial["issues"]]
                glossary_alerts = reused_initial.get("glossary_alerts", [])
                repaired = bool(reused_initial.get("json_repaired"))
                if repaired:
                    buffer_event(
                        "review_json_repaired",
                        start_index=base,
                        count=len(segs),
                    )
                # reused_initial is assigned only with nonempty debug data; narrow it explicitly for typing.
                if debug is not None and chapter_index is not None:
                    debug.record_initial_issues(
                        chapter=chapter_index,
                        chunk_base=base,
                        issues=local_issues,
                    )
                initial_issue_count = len(local_issues)
            else:

                def trace(event: str, data: dict[str, Any]) -> None:
                    """Persist initial review requests, raw responses or service errors
                    incrementally.
                    """
                    if debug is None or initial_trace is None:
                        return
                    initial_trace[event] = data
                    debug.write_json(initial_path, initial_trace)

                try:
                    object_contexts = (
                        evidence.object_contexts(chapter_index, base, len(segs))
                        if evidence is not None and chapter_index is not None
                        else []
                    )
                    review_result = self._reviewer.review_result(
                        srcs,
                        tgts,
                        terms,
                        trace=trace if debug is not None else None,
                        **({"object_contexts": object_contexts} if any(object_contexts) else {}),
                    )
                except Exception as error:
                    if debug is not None and initial_trace is not None:
                        initial_trace["status"] = "failed"
                        initial_trace["error"] = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                        if isinstance(error, ReviewOutputError):
                            initial_trace["error"]["reason"] = error.reason
                        debug.write_json(initial_path, initial_trace)
                    raise
                repaired = review_result.repaired
                if repaired:
                    buffer_event(
                        "review_json_repaired",
                        start_index=base,
                        count=len(segs),
                    )
                for it in review_result.issues:
                    it = dict(it)
                    idx = integer_index(it.get("index"))
                    if idx is not None and 0 <= idx < len(segs):
                        it["index"] = idx
                        local_issues.append(it)
                    else:
                        raise ReviewOutputError("invalid_issue_index")
                initial_issue_count = len(review_result.issues)
                glossary_alerts = review_result.glossary_alerts
                if debug is not None and initial_trace is not None:
                    initial_trace["status"] = "finished"
                    initial_trace["json_repaired"] = repaired
                    initial_trace["issues"] = local_issues
                    initial_trace["glossary_alerts"] = glossary_alerts
                    debug.write_json(initial_path, initial_trace)
                    if chapter_index is not None:
                        debug.record_initial_issues(
                            chapter=chapter_index,
                            chunk_base=base,
                            issues=local_issues,
                        )

            # Save initial issues before the evidence loop for the chunk cache.
            local_issues_before_agent = list(local_issues)
            dismissed: list[dict[str, Any]] = []
            fallback_reason = ""
            if (
                local_issues
                and evidence is not None
                and debug is not None
                and self._config.pipeline.review_agent_loop
                and chapter_index is not None
            ):
                outcome = ReviewAgentLoop(
                    self._client,
                    self._config,
                    evidence,
                    ReviewTraceStore(debug),
                ).review_chunk(
                    chapter=chapter_index,
                    chunk_base=base,
                    sources=srcs,
                    targets=tgts,
                    initial_issues=local_issues,
                    review_round=review_round,
                )
                local_issues = outcome.issues
                dismissed = outcome.dismissed
                fallback_reason = outcome.fallback_reason
                debug.record_dismissed(
                    chapter=chapter_index,
                    chunk_base=base,
                    issues=dismissed,
                )

            mapped: list[dict[str, Any]] = []
            for issue in local_issues:
                local_index = issue.get("index")
                if (
                    isinstance(local_index, int)
                    and not isinstance(local_index, bool)
                    and 0 <= local_index < len(segs)
                ):
                    issue = dict(issue)
                    issue["index"] = base + local_index
                    issue["_chunk_id"] = chunk_id
                    if fallback_reason:
                        issue["fallback_reason"] = fallback_reason
                    mapped.append(issue)
            if debug is not None:
                buffer_event(
                    "review_leaf_finished",
                    chunk_base=base,
                    segment_count=len(segs),
                    initial_issue_count=initial_issue_count,
                    final_issue_count=len(mapped),
                    dismissed_count=len(dismissed),
                    fallback=bool(fallback_reason),
                )
            # Persist chunk results inside review_once while complete data is available.
            if debug is not None and chapter_index is not None:
                debug.mark_chunk_done(
                    chunk_id,
                    {
                        "issues": mapped,
                        "initial_issues": local_issues_before_agent,
                        "dismissed": dismissed,
                        "fallback_reason": fallback_reason,
                        "glossary_alerts": [
                            {**a, "chapter": chapter_index, "index": base + a["index"]}
                            for a in glossary_alerts
                        ],
                    },
                )
            return mapped

        def review_adaptive(base: int, segs: list) -> list[dict]:
            """Shrink malformed requests; use bounded same-input retries only after reaching
            one paragraph.
            """
            try:
                return review_once(base, segs)
            except ReviewOutputError as error:
                if len(segs) > 1:
                    mid = len(segs) // 2
                    buffer_event(
                        "review_chunk_split",
                        start_index=base,
                        count=len(segs),
                        left_count=mid,
                        right_count=len(segs) - mid,
                        reason=error.reason,
                    )
                    return review_adaptive(base, segs[:mid]) + review_adaptive(
                        base + mid, segs[mid:]
                    )

                last_error = error
                retries = self._config.pipeline.review_output_retries
                for attempt in range(1, retries + 1):
                    buffer_event(
                        "review_singleton_retry",
                        start_index=base,
                        count=1,
                        attempt=attempt,
                        max_retries=retries,
                        reason=last_error.reason,
                    )
                    try:
                        result = review_once(base, segs, attempt=attempt + 1)
                    except ReviewOutputError as retry_error:
                        last_error = retry_error
                        continue
                    buffer_event(
                        "review_singleton_recovered",
                        start_index=base,
                        count=1,
                        attempt=attempt,
                    )
                    return result
                buffer_event(
                    "review_singleton_failed",
                    start_index=base,
                    count=1,
                    attempts=retries + 1,
                    reason=last_error.reason,
                )
                raise last_error

        return review_adaptive(chunk_base, chunk)

    def review_chapters(
        self,
        chapters,
        terms: list[GlossaryTerm],
        *,
        evidence: BookEvidenceIndex | None = None,
        debug: ReviewRunStore | None = None,
        target_overrides: Mapping[tuple[int, int], str] | None = None,
        review_round: int | None = None,
        on_chunk_finished: Callable[[int], None] | None = None,
        on_chapter_finished: Callable[[Any, list[dict]], None] | None = None,
        flush_usage: Callable[[], Any] | None = None,
    ) -> list[list[dict]]:
        """Review blocks across all chapters up to ``review_concurrency`` and merge in source order.

        Flattening chapter blocks into one bounded worker pool lets ``review_concurrency``
        parallelize single-chunk chapters without spawning nested pools, while streaming
        events and invoking ``on_chapter_finished`` strictly in source chapter order.
        When ``flush_usage`` is provided it runs on the calling thread after each completed
        chunk checkpoint so interrupted scans keep finished-chunk usage.
        """
        chapter_list = list(chapters)
        if not chapter_list:
            return []
        budget = self._config.segment.max_tokens_per_batch * 3
        all_jobs: list[tuple[int, int | None, int, int, list]] = []
        chapter_chunk_slots: list[list[list[dict] | None]] = []
        remaining_chunks: list[int] = []
        chapter_events: list[list[dict[str, Any]]] = [[] for _ in chapter_list]
        final_chapter_issues: list[list[dict]] = [[] for _ in chapter_list]

        for cpos, chapter in enumerate(chapter_list):
            chunks = self.pack_contiguous(chapter.text_segments, budget)
            chapter_chunk_slots.append([None] * len(chunks))
            remaining_chunks.append(len(chunks))
            base = 0
            for kpos, chunk in enumerate(chunks):
                all_jobs.append((cpos, chapter.index, kpos, base, chunk))
                base += len(chunk)

        buffer_lock = Lock()
        next_chapter_to_flush = 0

        def flush_chapter_events(cpos: int) -> None:
            if debug is None:
                return
            with buffer_lock:
                pending = order_buffered_events(chapter_events[cpos])
                chapter_events[cpos].clear()
            cidx = chapter_list[cpos].index
            for row in pending:
                event = row["event"]
                payload = {
                    "chapter": cidx,
                    **{key: value for key, value in row.items() if key != "event"},
                }
                debug.log_event(event, **payload)

        def flush_ready_chapters() -> None:
            nonlocal next_chapter_to_flush
            while (
                next_chapter_to_flush < len(chapter_list)
                and remaining_chunks[next_chapter_to_flush] == 0
            ):
                cpos = next_chapter_to_flush
                flush_chapter_events(cpos)
                merged = [
                    issue
                    for chunk_issues in chapter_chunk_slots[cpos]
                    if chunk_issues is not None
                    for issue in chunk_issues
                ]
                final_chapter_issues[cpos] = merged
                if on_chapter_finished is not None:
                    on_chapter_finished(chapter_list[cpos], merged)
                next_chapter_to_flush += 1

        def run_job(cpos: int, cidx: int | None, cbase: int, chunk: list) -> list[dict]:
            def buffer_event(event: str, **data: Any) -> None:
                with buffer_lock:
                    chapter_events[cpos].append({"event": event, **data})

            return self._review_chunk_adaptive(
                cbase,
                chunk,
                terms,
                chapter_index=cidx,
                evidence=evidence,
                debug=debug,
                target_overrides=target_overrides,
                review_round=review_round,
                buffer_event=buffer_event,
            )

        flush_ready_chapters()
        if not all_jobs:
            return final_chapter_issues

        workers = min(max(1, self._config.pipeline.review_concurrency), len(all_jobs))
        try:
            if workers == 1:
                for cpos, cidx, kpos, cbase, chunk in all_jobs:
                    chapter_chunk_slots[cpos][kpos] = run_job(cpos, cidx, cbase, chunk)
                    remaining_chunks[cpos] -= 1
                    if on_chunk_finished is not None:
                        on_chunk_finished(len(chunk))
                    if flush_usage is not None:
                        flush_usage()
                    flush_ready_chapters()
            else:
                job_iter = iter(all_jobs)
                in_flight: dict[Future[list[dict]], tuple[int, int, int]] = {}
                with ThreadPoolExecutor(max_workers=workers) as ex:

                    def submit_next() -> bool:
                        try:
                            cpos, cidx, kpos, cbase, chunk = next(job_iter)
                        except StopIteration:
                            return False
                        fut = ex.submit(run_job, cpos, cidx, cbase, chunk)
                        in_flight[fut] = (cpos, kpos, len(chunk))
                        return True

                    for _ in range(workers):
                        if not submit_next():
                            break

                    while in_flight:
                        done_futs, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                        ordered_done = sorted(done_futs, key=lambda fut: in_flight[fut][:2])
                        first_error: BaseException | None = None
                        for fut in ordered_done:
                            cpos, kpos, seg_count = in_flight.pop(fut)
                            try:
                                chunk_issues = fut.result()
                            except BaseException as exc:
                                if first_error is None:
                                    first_error = exc
                                continue
                            chapter_chunk_slots[cpos][kpos] = chunk_issues
                            remaining_chunks[cpos] -= 1
                            if on_chunk_finished is not None:
                                on_chunk_finished(seg_count)
                            if flush_usage is not None:
                                flush_usage()
                        flush_ready_chapters()
                        if first_error is not None:
                            for pending_fut in in_flight:
                                pending_fut.cancel()
                            raise first_error
                        while len(in_flight) < workers and submit_next():
                            pass
        finally:
            # All worker threads have exited before flushing remaining diagnostic events.
            for cpos in range(next_chapter_to_flush, len(chapter_list)):
                flush_chapter_events(cpos)
        return final_chapter_issues

    def review_chapter(
        self,
        text_segs,
        terms: list[GlossaryTerm],
        *,
        chapter_index: int | None = None,
        evidence: BookEvidenceIndex | None = None,
        debug: ReviewRunStore | None = None,
        target_overrides: Mapping[tuple[int, int], str] | None = None,
        review_round: int | None = None,
        on_chunk_finished: Callable[[int], None] | None = None,
        flush_usage: Callable[[], Any] | None = None,
    ) -> list[dict]:
        """Review contiguous chapter blocks in parallel and return chapter-local issue indices.
        Use blocks around three translation batches to reduce calls and repeated context.
        Convert valid block-local indices by the block offset and reject invalid positions.
        Supply the full entity glossary (plus navigation titles matching the chunk, fitted
        to the model context window) to every fresh reviewer request and share the same
        snapshot across workers. Completed chunks and initial traces bypass model calls.
        Read fixed target/glossary snapshots. Recursively bisect invalid output and retry
        single paragraphs a bounded number of times. Merge results in original block order
        for determinism. When ``flush_usage`` is provided it runs on the calling thread after
        each completed chunk checkpoint so interrupted scans keep finished-chunk usage.
        """
        if not text_segs:
            return []

        return self.review_chapters(
            [SimpleNamespace(index=chapter_index, text_segments=text_segs)],
            terms,
            evidence=evidence,
            debug=debug,
            target_overrides=target_overrides,
            review_round=review_round,
            on_chunk_finished=on_chunk_finished,
            flush_usage=flush_usage,
        )[0]

    @staticmethod
    def try_cached_subchunks(
        chunk_base: int,
        chunk: list,
        debug: ReviewRunStore,
        round_prefix: str,
        chapter_index: int | None,
    ) -> list[dict] | None:
        """Probe child chunk caches recursively using review_adaptive's bisection.
        Like translation resume boundaries, inspect children when a parent cache misses. If
        every child is cached, merge them and skip the reviewer call. Do not write
        initial/dismissed snapshots while probing; persist only after the complete subtree
        matches, preventing double counts when a partially cached parent must rerun.
        """
        hits: list[tuple[int, dict[str, Any]]] = []

        def probe(base: int, pieces: list) -> list[dict] | None:
            if not pieces:
                return []
            chunk_id = f"{round_prefix}ch{chapter_index}-base{base}-n{len(pieces)}"
            cached = debug.load_chunk_result(chunk_id)
            if cached is not None:
                hits.append((base, cached))
                return list(cached.get("issues", []))
            if len(pieces) <= 1:
                return None
            mid = len(pieces) // 2
            left = probe(base, pieces[:mid])
            if left is None:
                return None
            right = probe(base + mid, pieces[mid:])
            if right is None:
                return None
            return left + right

        merged = probe(chunk_base, chunk)
        if merged is None:
            return None
        if chapter_index is not None:
            for base, cached in hits:
                debug.record_initial_issues(
                    chapter=chapter_index,
                    chunk_base=base,
                    issues=cached.get("initial_issues", []),
                )
                debug.record_dismissed(
                    chapter=chapter_index,
                    chunk_base=base,
                    issues=cached.get("dismissed", []),
                )
        return merged

    @staticmethod
    def pack_contiguous(segs, budget: int) -> list[list]:
        """Pack paragraphs into contiguous blocks by source-token budget without changing
        order.
        """
        chunks: list[list] = []
        cur: list = []
        size = 0
        for s in segs:
            tokens = count_tokens(s.source)
            if cur and size + tokens > budget:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(s)
            size += tokens
        if cur:
            chunks.append(cur)
        return chunks
