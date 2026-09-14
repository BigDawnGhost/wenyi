"""Execute one whole-book scan/arbitration round and propose its shadow revisions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..agents.review_arbiter import ReviewConflictArbiter
from ..agents.review_fixer import ProvisionalPatch, ReviewFixer, ReviewFixerProtocolError
from ..config import Config
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..llm.base import LLMClient
from ..review.conflicts import (
    apply_review_arbitrations,
    build_conflict_groups,
    normalize_review_issues,
)
from ..review.evidence import BookEvidenceIndex
from ..review.run_store import ReviewRunStore
from ..review.session import ReviewRoundResult
from . import review_results
from .review_checkpoint import ReviewTraceStore
from .review_chunks import ReviewChunkService

ProgressFn = Callable[[int, int, str], None]


class ReviewRoundService:
    """Run a fixed shadow view using explicit model and chunk collaborators."""

    def __init__(
        self,
        config: Config,
        client: LLMClient,
        style_brief: Callable[[dict[str, Any]], str],
        chunks: ReviewChunkService,
    ):
        self._config = config
        self._client = client
        self._style_brief = style_brief
        self._chunks = chunks

    def review_round(
        self,
        loaded,
        all_terms: list[GlossaryTerm],
        evidence: BookEvidenceIndex,
        debug: ReviewRunStore,
        *,
        review_round: int,
        target_overrides: Mapping[tuple[int, int], str],
        progress: ProgressFn | None = None,
    ) -> ReviewRoundResult:
        """Review and arbitrate one immutable whole-book shadow snapshot."""
        total = sum(len(chapter.text_segments) for chapter in loaded)
        done = 0
        review_label = (
            f"Whole-book review R{review_round}"
            if review_round == 1
            else f"Blind whole-book review R{review_round}"
        )
        if progress:
            progress(0, total, review_label)
        raw_issues: list[dict[str, Any]] = []
        for chapter in loaded:
            text_segs = chapter.text_segments

            def on_chunk_finished(segment_count: int) -> None:
                """Advance this round's paragraph progress after a top-level review block
                completes.
                """
                nonlocal done
                done += segment_count
                if progress:
                    progress(done, total, review_label)

            chapter_issues = self._chunks.review_chapter(
                text_segs,
                all_terms,
                chapter_index=chapter.index,
                evidence=evidence,
                debug=debug,
                target_overrides=target_overrides,
                review_round=review_round,
                on_chunk_finished=on_chunk_finished,
            )
            for issue in chapter_issues:
                issue["chapter"] = chapter.index
                issue["stage"] = "review_agent"
                issue["review_round"] = review_round
            raw_issues.extend(chapter_issues)
            debug.log_event(
                "review_chapter_finished",
                chapter=chapter.index,
                segment_count=len(text_segs),
                issue_count=len(chapter_issues),
            )

        pre_arbitration_issues = normalize_review_issues(raw_issues, evidence)
        for issue in pre_arbitration_issues:
            issue["issue_id"] = f"r{review_round}-{issue['issue_id']}"
        conflict_groups = build_conflict_groups(pre_arbitration_issues)
        arbitrations: list[dict[str, Any]] = []
        if conflict_groups and self._config.pipeline.review_conflict_arbitration:
            arbitration_label = f"Conflict arbitration R{review_round}"
            arbitration_total = len(conflict_groups)
            if progress:
                progress(0, arbitration_total, arbitration_label)
            workers = min(
                max(1, self._config.pipeline.review_concurrency),
                arbitration_total,
            )

            def arbitrate(group: dict[str, Any]) -> dict[str, Any]:
                return ReviewConflictArbiter(
                    self._client,
                    self._config,
                    evidence,
                    ReviewTraceStore(debug),
                ).arbitrate(group)

            if workers == 1:
                for done_count, group in enumerate(conflict_groups, start=1):
                    arbitrations.append(arbitrate(group))
                    if progress:
                        progress(done_count, arbitration_total, arbitration_label)
            else:
                ordered_arbitrations: list[dict[str, Any] | None] = [None] * arbitration_total
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(arbitrate, group): position
                        for position, group in enumerate(conflict_groups)
                    }
                    for done_count, future in enumerate(as_completed(futures), start=1):
                        ordered_arbitrations[futures[future]] = future.result()
                        if progress:
                            progress(done_count, arbitration_total, arbitration_label)
                arbitrations = [
                    arbitration for arbitration in ordered_arbitrations if arbitration is not None
                ]
        elif conflict_groups:
            arbitrations = [
                {
                    "conflict_id": group["conflict_id"],
                    "consistency_key": group["consistency_key"],
                    "issue_ids": [issue["issue_id"] for issue in group["issues"]],
                    "status": "unresolved",
                    "recommended_value": "",
                    "reason": "Whole-book conflict arbitration is disabled in configuration.",
                    "supported_issue_ids": [issue["issue_id"] for issue in group["issues"]],
                    "rejected_issue_ids": [],
                    "evidence_refs": [],
                }
                for group in conflict_groups
            ]

        final_issues, arbitration_superseded = apply_review_arbitrations(
            pre_arbitration_issues,
            arbitrations,
        )
        fallback_agent_count = len(
            {
                issue["_chunk_id"]
                for issue in pre_arbitration_issues
                if issue.get("agent_fallback") and isinstance(issue.get("_chunk_id"), str)
            }
        )
        residual_conflicts = build_conflict_groups(final_issues)
        initial_issues, dismissed = debug.result_snapshots(review_round)
        debug.write_json("initial_issues.json", initial_issues)
        debug.write_json("dismissed_issues.json", dismissed)
        debug.write_json("pre_arbitration_issues.json", pre_arbitration_issues)
        debug.write_json("arbitration_superseded_issues.json", arbitration_superseded)
        debug.write_json("final_issues.json", final_issues)
        debug.write_json(
            "residual_conflicts.json",
            [
                {
                    "conflict_id": group["conflict_id"],
                    "consistency_key": group["consistency_key"],
                    "issue_ids": [issue["issue_id"] for issue in group["issues"]],
                }
                for group in residual_conflicts
            ],
        )
        debug.write_json(
            "conflicts.json",
            review_results.conflict_records(conflict_groups, arbitrations),
        )
        debug.log_event(
            "review_round_finished",
            issue_count=len(final_issues),
            conflict_count=len(conflict_groups),
            unresolved_conflict_count=len(residual_conflicts),
            fallback_agent_count=fallback_agent_count,
        )
        return ReviewRoundResult(
            issues=final_issues,
            pre_arbitration_issues=pre_arbitration_issues,
            arbitration_superseded=arbitration_superseded,
            conflict_groups=conflict_groups,
            residual_conflicts=residual_conflicts,
            fallback_agent_count=fallback_agent_count,
        )

    def propose_review_patches(
        self,
        round_result: ReviewRoundResult,
        evidence: BookEvidenceIndex,
        all_terms: list[GlossaryTerm],
        analysis: dict[str, Any],
        debug: ReviewRunStore,
        *,
        review_round: int,
        fix_round: int,
        progress: ProgressFn | None = None,
    ) -> tuple[list[ProvisionalPatch], list[dict[str, Any]]]:
        """Group confirmed issues by paragraph and generate complete replacements for the next
        round.
        """
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        skipped: list[dict[str, Any]] = []
        for issue in round_result.issues:
            chapter = issue.get("chapter")
            index = issue.get("index")
            issue_id = issue.get("issue_id")
            if (
                isinstance(chapter, bool)
                or not isinstance(chapter, int)
                or isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(issue_id, str)
            ):
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "status": "skipped",
                        "reason": "invalid_issue_location",
                    }
                )
                continue
            arbitration = issue.get("arbitration")
            if isinstance(arbitration, dict) and arbitration.get("status") == "unresolved":
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "chapter": chapter,
                        "index": index,
                        "status": "skipped",
                        "reason": "unresolved_consistency_conflict",
                    }
                )
                continue
            if self._config.pipeline.review_agent_loop and issue.get("agent_fallback"):
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "chapter": chapter,
                        "index": index,
                        "status": "skipped",
                        "reason": "unverified_agent_fallback",
                    }
                )
                continue
            grouped.setdefault((chapter, index), []).append(issue)

        jobs = sorted(grouped.items())
        if not jobs:
            return [], skipped
        fix_label = f"Shadow revision R{fix_round}"
        fix_total = len(jobs)
        if progress:
            progress(0, fix_total, fix_label)
        style = self._style_brief(analysis)
        book_synopsis = str(analysis.get("book_synopsis", "") or "")
        fixer = ReviewFixer(self._client, self._config)

        def propose(
            job: tuple[tuple[int, int], list[dict[str, Any]]],
        ) -> tuple[ProvisionalPatch | None, dict[str, Any] | None]:
            (chapter, index), issues = job
            segment = evidence.segment_ref(chapter, index)
            if segment is None:
                return None, {
                    "issue_ids": [issue["issue_id"] for issue in issues],
                    "chapter": chapter,
                    "index": index,
                    "status": "skipped",
                    "reason": "segment_not_found",
                }
            context = evidence.segment_context(
                {
                    "chapter": chapter,
                    "index": index,
                    "before": 4,
                    "after": 4,
                }
            )
            context_segments = context.get("segments", []) if context.get("ok") else []
            nearby_pairs = [
                (str(item.get("source", "")), str(item.get("target", "")))
                for item in context_segments
                if isinstance(item, dict) and item.get("ref") != segment.ref
            ]
            context_source = "\n".join(
                str(item.get("source", "")) for item in context_segments if isinstance(item, dict)
            )
            relevant_terms = GlossaryStore.terms_in(
                all_terms,
                context_source or segment.source,
            )
            trace_path = f"fixers/ch{chapter}-text{index}.json"
            existing = debug.load_json(trace_path)
            if existing is not None and existing.get("status") == "finished":
                cached = existing.get("patch")
                if isinstance(cached, dict) and isinstance(cached.get("issue_ids"), list):
                    try:
                        saved_patch = ProvisionalPatch(
                            **{**cached, "issue_ids": tuple(cached["issue_ids"])}
                        )
                    except TypeError:
                        saved_patch = None
                    if (
                        saved_patch is not None
                        and saved_patch.round == review_round
                        and saved_patch.segment_ref == segment.ref
                        and (saved_patch.chapter, saved_patch.index) == (chapter, index)
                        and saved_patch.before == segment.target
                        and saved_patch.before_hash == ReviewFixer.target_hash(segment.target)
                        and all(isinstance(issue_id, str) for issue_id in saved_patch.issue_ids)
                        and set(saved_patch.issue_ids) == {issue["issue_id"] for issue in issues}
                        and isinstance(saved_patch.after, str)
                        and saved_patch.after.strip()
                        and saved_patch.status == "provisional"
                    ):
                        return saved_patch, None
            trace: dict[str, Any] = {
                "chapter": chapter,
                "index": index,
                "segment_ref": segment.ref,
                "issue_ids": [issue["issue_id"] for issue in issues],
                "status": "running",
            }
            debug.write_json(trace_path, trace)

            def record(event: str, data: dict[str, Any]) -> None:
                trace[event] = data
                debug.write_json(trace_path, trace)

            try:
                patch = fixer.propose(
                    review_round,
                    segment.ref,
                    chapter,
                    index,
                    segment.source,
                    segment.target,
                    issues,
                    style=style,
                    book_synopsis=book_synopsis,
                    chapter_digest=evidence.chapter_digests.get(chapter, ""),
                    relevant_glossary=relevant_terms,
                    nearby_pairs=nearby_pairs,
                    trace=record,
                )
            except Exception as error:  # noqa: BLE001 - Keep individual fixer failures as unresolved suggestions.
                trace["status"] = "failed"
                trace["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                debug.write_json(trace_path, trace)
                return None, {
                    "issue_ids": [issue["issue_id"] for issue in issues],
                    "chapter": chapter,
                    "index": index,
                    "segment_ref": segment.ref,
                    "status": "failed",
                    "reason": (
                        str(error)
                        if isinstance(error, ReviewFixerProtocolError)
                        else f"{type(error).__name__}: {error}"
                    ),
                }
            trace["status"] = "finished"
            trace["patch"] = patch.as_dict()
            debug.write_json(trace_path, trace)
            return patch, None

        workers = min(
            max(1, self._config.pipeline.review_concurrency),
            fix_total,
        )
        if workers == 1:
            results = []
            for done_count, job in enumerate(jobs, start=1):
                results.append(propose(job))
                if progress:
                    progress(done_count, fix_total, fix_label)
        else:
            ordered_results: list[tuple[ProvisionalPatch | None, dict[str, Any] | None] | None] = [
                None
            ] * fix_total
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(propose, job): position for position, job in enumerate(jobs)
                }
                for done_count, future in enumerate(as_completed(futures), start=1):
                    ordered_results[futures[future]] = future.result()
                    if progress:
                        progress(done_count, fix_total, fix_label)
            results = [result for result in ordered_results if result is not None]
        patches = [patch for patch, _ in results if patch is not None]
        failures = [failure for _, failure in results if failure is not None]
        debug.log_event(
            "review_fix_round_finished",
            patch_count=len(patches),
            skipped_count=len(skipped),
            failed_count=len(failures),
        )
        return patches, [*skipped, *failures]
