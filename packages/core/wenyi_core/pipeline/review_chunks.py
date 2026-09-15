"""Chapter-level Review scanning, lazy terminology, adaptive recovery and stable merging."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any

from ..agents.review_loop import ReviewAgentLoop
from ..agents.reviewer import Reviewer, ReviewOutputError
from ..config import Config
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.tokens import count_tokens
from ..llm.base import LLMClient
from ..review.evidence import BookEvidenceIndex
from ..review.run_store import ReviewRunStore
from .review_checkpoint import ReviewTraceStore


class ReviewChunkService:
    """Execute fixed chapter snapshots without owning session policy or book state."""

    def __init__(self, config: Config, client: LLMClient, reviewer: Reviewer):
        self._config = config
        self._client = client
        self._reviewer = reviewer

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
    ) -> list[dict]:
        """Review contiguous chapter blocks in parallel and return chapter-local issue indices.
        Use blocks around three translation batches to reduce calls and repeated context.
        Convert valid block-local indices by the block offset and reject invalid positions.
        Filter the chapter glossary only when a fresh reviewer request needs it; completed
        chunks and initial traces bypass matching. Share one snapshot across workers.
        Read fixed target/glossary snapshots. Recursively bisect malformed output and retry
        single paragraphs a bounded number of times. Merge results in original block order
        for determinism.
        """
        budget = self._config.segment.max_tokens_per_batch * 3
        chunks = self.pack_contiguous(text_segs, budget)
        if not chunks:
            return []

        jobs: list[tuple[int, list]] = []
        base = 0
        for chunk in chunks:
            jobs.append((base, chunk))
            base += len(chunk)

        recovery_events: list[dict[str, Any]] = []
        recovery_lock = Lock()
        term_snapshot: list[GlossaryTerm] | None = None
        term_lock = Lock()

        def reviewer_terms() -> list[GlossaryTerm]:
            """Build the chapter-wide glossary once, after all reusable caches miss."""
            nonlocal term_snapshot
            if self._config.pipeline.glossary_scope != "chapter":
                return terms
            with term_lock:
                if term_snapshot is None:
                    source_text = "\n".join(segment.source for segment in text_segs)
                    term_snapshot = GlossaryStore.terms_in(terms, source_text)
                return term_snapshot

        def record_recovery(event: str, **data: Any) -> None:
            """Buffer recovery events under a lock; write them from the main thread after
            workers finish.
            """
            with recovery_lock:
                recovery_events.append({"event": event, **data})

        def review_once(chunk_base: int, chunk: list, *, attempt: int = 1) -> list[dict]:
            """Run one review call and map valid block-local indices to chapter indices."""
            srcs = [s.source for s in chunk]
            overrides = target_overrides or {}

            def target_for(local_index: int, segment) -> str:
                """Read this round's shadow target, falling back to formal text when chapter
                position is unavailable.
                """
                if chapter_index is None:
                    return segment.target or ""
                return overrides.get(
                    (chapter_index, chunk_base + local_index),
                    segment.target or "",
                )

            tgts = [target_for(local_index, segment) for local_index, segment in enumerate(chunk)]

            # Check the chunk cache to skip reviewer and evidence-loop model calls on resume.
            round_prefix = f"r{review_round}-" if review_round is not None else ""
            chunk_id = f"{round_prefix}ch{chapter_index}-base{chunk_base}-n{len(chunk)}"
            if debug is not None:
                cached = debug.load_chunk_result(chunk_id)
                if cached is not None:
                    # Restore initial/dismissed aggregation needed by the report.
                    if chapter_index is not None:
                        debug.record_initial_issues(
                            chapter=chapter_index,
                            chunk_base=chunk_base,
                            issues=cached.get("initial_issues", []),
                        )
                        debug.record_dismissed(
                            chapter=chapter_index,
                            chunk_base=chunk_base,
                            issues=cached.get("dismissed", []),
                        )
                    return cached.get("issues", [])

            # Probe child chunk caches using boundaries compatible with adaptive recovery.
            # Match review_adaptive's recursive bisection. If every child is cached,
            # merge them directly without making a reviewer call.
            if debug is not None and len(chunk) > 1:
                cached_sub = self.try_cached_subchunks(
                    chunk_base,
                    chunk,
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
                    f"initial-{round_prefix}ch{chapter_index}-base{chunk_base}"
                    f"-n{len(chunk)}-attempt{attempt}"
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
                    initial_trace = {
                        "agent_id": initial_id,
                        "chapter": chapter_index,
                        "chunk_base": chunk_base,
                        "segment_count": len(chunk),
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
                repaired = bool(reused_initial.get("json_repaired"))
                if repaired:
                    record_recovery(
                        "review_json_repaired",
                        start_index=chunk_base,
                        count=len(chunk),
                    )
                # reused_initial is assigned only with nonempty debug data; narrow it explicitly for typing.
                if debug is not None and chapter_index is not None:
                    debug.record_initial_issues(
                        chapter=chapter_index,
                        chunk_base=chunk_base,
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
                    review_result = self._reviewer.review_result(
                        srcs,
                        tgts,
                        reviewer_terms(),
                        trace=trace if debug is not None else None,
                    )
                except Exception as error:
                    if debug is not None and initial_trace is not None:
                        initial_trace["status"] = "failed"
                        initial_trace["error"] = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                        debug.write_json(initial_path, initial_trace)
                    raise
                repaired = review_result.repaired
                if repaired:
                    record_recovery(
                        "review_json_repaired",
                        start_index=chunk_base,
                        count=len(chunk),
                    )
                for it in review_result.issues:
                    it = dict(it)
                    idx = it.get("index")
                    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(chunk):
                        it["index"] = idx
                        local_issues.append(it)
                    else:
                        raise ReviewOutputError("invalid_issue_index")
                initial_issue_count = len(review_result.issues)
                if debug is not None and initial_trace is not None:
                    initial_trace["status"] = "finished"
                    initial_trace["json_repaired"] = repaired
                    initial_trace["issues"] = local_issues
                    debug.write_json(initial_path, initial_trace)
                    if chapter_index is not None:
                        debug.record_initial_issues(
                            chapter=chapter_index,
                            chunk_base=chunk_base,
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
                    chunk_base=chunk_base,
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
                    chunk_base=chunk_base,
                    issues=dismissed,
                )

            mapped: list[dict[str, Any]] = []
            for issue in local_issues:
                local_index = issue.get("index")
                if (
                    isinstance(local_index, int)
                    and not isinstance(local_index, bool)
                    and 0 <= local_index < len(chunk)
                ):
                    issue = dict(issue)
                    issue["index"] = chunk_base + local_index
                    issue["_chunk_id"] = chunk_id
                    if fallback_reason:
                        issue["fallback_reason"] = fallback_reason
                    mapped.append(issue)
            if debug is not None:
                debug.log_event(
                    "review_leaf_finished",
                    chapter=chapter_index,
                    chunk_base=chunk_base,
                    segment_count=len(chunk),
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
                    },
                )
            return mapped

        def review_adaptive(chunk_base: int, chunk: list) -> list[dict]:
            """Shrink malformed requests; use bounded same-input retries only after reaching
            one paragraph.
            """
            try:
                return review_once(chunk_base, chunk)
            except ReviewOutputError as error:
                if len(chunk) > 1:
                    mid = len(chunk) // 2
                    record_recovery(
                        "review_chunk_split",
                        start_index=chunk_base,
                        count=len(chunk),
                        left_count=mid,
                        right_count=len(chunk) - mid,
                        reason=error.reason,
                    )
                    return review_adaptive(chunk_base, chunk[:mid]) + review_adaptive(
                        chunk_base + mid, chunk[mid:]
                    )

                last_error = error
                retries = self._config.pipeline.review_output_retries
                for attempt in range(1, retries + 1):
                    record_recovery(
                        "review_singleton_retry",
                        start_index=chunk_base,
                        count=1,
                        attempt=attempt,
                        max_retries=retries,
                        reason=last_error.reason,
                    )
                    try:
                        result = review_once(chunk_base, chunk, attempt=attempt + 1)
                    except ReviewOutputError as retry_error:
                        last_error = retry_error
                        continue
                    record_recovery(
                        "review_singleton_recovered",
                        start_index=chunk_base,
                        count=1,
                        attempt=attempt,
                    )
                    return result
                record_recovery(
                    "review_singleton_failed",
                    start_index=chunk_base,
                    count=1,
                    attempts=retries + 1,
                    reason=last_error.reason,
                )
                raise last_error

        def review_one(job: tuple[int, list]) -> list[dict]:
            """Review one initial contiguous block with local recovery as needed."""
            chunk_base, chunk = job
            return review_adaptive(chunk_base, chunk)

        workers = min(
            max(1, self._config.pipeline.review_concurrency),
            len(jobs),
        )
        try:
            if workers == 1:
                results = []
                for job in jobs:
                    results.append(review_one(job))
                    if on_chunk_finished:
                        on_chunk_finished(len(job[1]))
            else:
                ordered_results: list[list[dict] | None] = [None] * len(jobs)
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {
                        ex.submit(review_one, job): (position, len(job[1]))
                        for position, job in enumerate(jobs)
                    }
                    for future in as_completed(futures):
                        position, segment_count = futures[future]
                        ordered_results[position] = future.result()
                        if on_chunk_finished:
                            on_chunk_finished(segment_count)
                results = [result for result in ordered_results if result is not None]
        finally:
            if debug is not None:
                with recovery_lock:
                    event_order = {
                        "review_json_repaired": 0,
                        "review_chunk_split": 0,
                        "review_singleton_retry": 1,
                        "review_singleton_recovered": 2,
                        "review_singleton_failed": 2,
                    }
                    pending_events = sorted(
                        recovery_events,
                        key=lambda row: (
                            row.get("start_index", -1),
                            -row.get("count", 0),
                            event_order.get(row.get("event", ""), 99),
                            row.get("attempt", 0),
                        ),
                    )
                for row in pending_events:
                    event = row["event"]
                    payload = {
                        "chapter": chapter_index,
                        **{key: value for key, value in row.items() if key != "event"},
                    }
                    debug.log_event(event, **payload)
        return [issue for chunk_issues in results for issue in chunk_issues]

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
