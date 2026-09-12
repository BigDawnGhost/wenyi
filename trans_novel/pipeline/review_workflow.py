"""Read-only shadow review with parallel blocks, evidence, arbitration, revisions and blind
rechecks.
Each review uses an independent directory and updates only its shadow overlay. Formal
chapters, manifest and glossary stay read-only. Restore output order by input position and
write sorted recovery events outside worker threads. Persist failed/partial results,
diagnostics and usage before propagating top-level exceptions; individual fixer failures
remain unresolved issues.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from ..agents.review_fixer import (
    ProvisionalPatch,
    ReviewFixer,
    ReviewFixerProtocolError,
)
from ..agents.review_loop import (
    ReviewAgentLoop,
    ReviewConflictArbiter,
    apply_review_arbitrations,
    build_conflict_groups,
    normalize_review_issues,
)
from ..agents.reviewer import ReviewOutputError
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..i18n.resources import prompt_fingerprint
from ..llm.retrying import is_resumable_provider_interrupt
from ..review.evidence import BookEvidenceIndex
from ..review.run_store import ReviewOutcome, ReviewRunStore
from .runstore import STATUS_DONE

if TYPE_CHECKING:
    from .runstore import RunStore
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


@dataclass(frozen=True)
class _ReviewRoundResult:
    """Deterministic result of one whole-book shadow review and conflict arbitration."""

    issues: list[dict[str, Any]]
    pre_arbitration_issues: list[dict[str, Any]]
    arbitration_superseded: list[dict[str, Any]]
    conflict_groups: list[dict[str, Any]]
    residual_conflicts: list[dict[str, Any]]
    fallback_agent_count: int


def _review_overlay_digest(
    chapters,
    overrides: Mapping[tuple[int, int], str],
) -> str:
    """Fingerprint effective shadow text to detect no progress and A/B oscillation."""
    payload = [
        (
            chapter.index,
            text_index,
            overrides.get((chapter.index, text_index), segment.target or ""),
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_content_digest(chapters) -> str:
    """Hash the formal body text actually read by this review."""
    payload = [
        (
            chapter.index,
            text_index,
            segment.index,
            segment.anchor or "",
            segment.kind,
            segment.source,
            segment.target or "",
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_net_changes(
    chapters,
    overrides: Mapping[tuple[int, int], str],
    patch_records: list[dict[str, Any]],
    active_patches: Mapping[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse multiple rounds of shadow patches into one final suggestion per paragraph."""
    baseline = {
        (chapter.index, text_index): segment.target or ""
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    }
    issue_keys_by_location: dict[tuple[int, int], set[str]] = {}
    for patch in patch_records:
        chapter = patch.get("chapter")
        index = patch.get("index")
        if (
            not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or patch.get("status") == "rejected_cycle"
        ):
            continue
        keys = issue_keys_by_location.setdefault((chapter, index), set())
        keys.update(str(key) for key in patch.get("issue_keys", []) if isinstance(key, str) and key)

    changes: list[dict[str, Any]] = []
    for location, suggested_target in sorted(overrides.items()):
        if baseline.get(location) == suggested_target:
            continue
        active = active_patches.get(location) or {}
        changes.append(
            {
                "chapter": location[0],
                "index": location[1],
                "suggested_target": suggested_target,
                "issue_keys": sorted(issue_keys_by_location.get(location, set())),
                "review_result": str(active.get("status") or "provisional"),
            }
        )
    return changes


def _review_public_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove internal review fields to produce stable user-facing issues."""
    public: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_key = issue.get("issue_key")
        chapter = issue.get("chapter")
        index = issue.get("index")
        if (
            not isinstance(issue_key, str)
            or not issue_key
            or not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            continue
        public[issue_key] = {
            "issue_key": issue_key,
            "chapter": chapter,
            "index": index,
            "type": str(issue.get("type") or ""),
            "detail": str(issue.get("detail") or ""),
            "suggestion": str(issue.get("suggestion") or ""),
        }
    return sorted(
        public.values(),
        key=lambda issue: (issue["chapter"], issue["index"], issue["issue_key"]),
    )


def _review_conflict_records(
    groups: list[dict[str, Any]],
    arbitrations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Serialize conflicts and arbitration decisions into stable per-round records."""
    return [
        {
            "conflict_id": group["conflict_id"],
            "consistency_key": group["consistency_key"],
            "issue_ids": [issue["issue_id"] for issue in group["issues"]],
            "proposals": [
                {
                    "issue_id": issue["issue_id"],
                    "chapter": issue["chapter"],
                    "index": issue["index"],
                    "proposed_value": issue["consistency"]["proposed_value"],
                }
                for issue in group["issues"]
            ],
            "arbitration": arbitration,
        }
        for group, arbitration in zip(groups, arbitrations)
    ]


def _review_unresolved_conflict_records(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild conflicts from final unresolved issues so an empty last round cannot hide them."""
    groups = build_conflict_groups(issues)
    arbitrations: list[dict[str, Any]] = []
    for group in groups:
        issue_ids = [str(issue["issue_id"]) for issue in group["issues"]]
        annotations = [
            issue.get("arbitration")
            for issue in group["issues"]
            if isinstance(issue.get("arbitration"), dict)
        ]
        reasons = [
            str(annotation.get("reason", "")).strip()
            for annotation in annotations
            if str(annotation.get("reason", "")).strip()
        ]
        evidence_refs = sorted(
            {
                str(ref)
                for issue in group["issues"]
                for ref in issue.get("evidence_refs", [])
                if isinstance(ref, str) and ref
            }
        )
        arbitrations.append(
            {
                "conflict_id": group["conflict_id"],
                "consistency_key": group["consistency_key"],
                "issue_ids": issue_ids,
                "status": "unresolved",
                "recommended_value": "",
                "reason": reasons[-1]
                if reasons
                else "Final unresolved issues still contain conflicting proposals.",
                "supported_issue_ids": issue_ids,
                "rejected_issue_ids": [],
                "evidence_refs": evidence_refs,
            }
        )
    return _review_conflict_records(groups, arbitrations)


def _review_unresolved_fallback_count(issues: list[dict[str, Any]]) -> int:
    """Count distinct degraded review blocks still represented in unresolved issues."""
    return len(
        {
            str(issue.get("_chunk_id") or issue.get("issue_key") or issue.get("issue_id"))
            for issue in issues
            if issue.get("agent_fallback")
        }
    )


class ReviewService:
    """Domain service for read-only whole-book agent review."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    def session_terms(
        self,
        store: RunStore,
        glossary: GlossaryStore | None = None,
    ) -> list[GlossaryTerm]:
        """Return the final glossary snapshot used by this review."""
        if glossary is not None:
            return glossary.all_terms()
        return GlossaryStore.load_terms_readonly(store.glossary_path)

    def _review_config_snapshot(self) -> dict[str, Any]:
        """Snapshot review configuration for persisted metadata and reuse checks."""
        from ..llm.operations import configured_operations
        from ..llm.routing import inference_snapshot

        return {
            "source_lang": self._runtime.config.source_lang,
            "target_lang": self._runtime.config.target_lang,
            "honorific_strategy": self._runtime.config.honorific_strategy,
            "prompt_fingerprint": prompt_fingerprint(),
            "review_output_retries": self._runtime.config.pipeline.review_output_retries,
            "review_agent_loop": self._runtime.config.pipeline.review_agent_loop,
            "inference": inference_snapshot(
                self._runtime.llm_config,
                (
                    operation
                    for operation in configured_operations(self._runtime.config, "review")
                    if operation.startswith("review.")
                ),
            ),
            "review_agent_max_evidence_rounds": (
                self._runtime.config.pipeline.review_agent_max_evidence_rounds
            ),
            "review_conflict_arbitration": (
                self._runtime.config.pipeline.review_conflict_arbitration
            ),
            "review_fix_loop": self._runtime.config.pipeline.review_fix_loop,
            "review_fix_max_rounds": self._runtime.config.pipeline.review_fix_max_rounds,
            "review_clean_confirmations": (
                self._runtime.config.pipeline.review_clean_confirmations
            ),
        }

    @staticmethod
    def _review_glossary_fingerprint(terms: list[GlossaryTerm]) -> str:
        """Fingerprint glossary content so changed terms invalidate completed review reuse."""
        ordered = sorted((term.source, term.target, term.type) for term in terms)
        return hashlib.sha256(json.dumps(ordered, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _review_skip_eligible(
        self,
        store: RunStore,
        latest: dict[str, Any],
        terms: list[GlossaryTerm],
    ) -> bool:
        """Reuse a completed review only when content, configuration and glossary all match."""
        review_id = latest.get("review_id")
        if not isinstance(review_id, str) or not review_id:
            return False
        metadata_path = os.path.join(store.run_dir, "reviews", review_id, "rounds", "metadata.json")
        try:
            with open(metadata_path, encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        saved_config = metadata.get("config")
        saved_glossary = metadata.get("glossary_fingerprint")
        return (
            isinstance(saved_config, dict)
            and saved_config == self._review_config_snapshot()
            and isinstance(saved_glossary, str)
            and saved_glossary == self._review_glossary_fingerprint(terms)
        )

    @staticmethod
    def _review_usage_from_dir(store: RunStore, review_id: str) -> dict[str, Any]:
        """Read usage from a completed review directory; return empty when unavailable."""
        try:
            with open(
                os.path.join(store.run_dir, "reviews", review_id, "usage.json"),
                encoding="utf-8",
            ) as f:
                usage = json.load(f)
            return usage if isinstance(usage, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

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
    ) -> _ReviewRoundResult:
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

            chapter_issues = self.review_chapter(
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
        if conflict_groups and self._runtime.config.pipeline.review_conflict_arbitration:
            arbitration_label = f"Conflict arbitration R{review_round}"
            arbitration_total = len(conflict_groups)
            if progress:
                progress(0, arbitration_total, arbitration_label)
            workers = min(
                max(1, self._runtime.config.pipeline.review_concurrency),
                arbitration_total,
            )

            def arbitrate(group: dict[str, Any]) -> dict[str, Any]:
                return ReviewConflictArbiter(
                    self._runtime.client,
                    self._runtime.config,
                    evidence,
                    debug,
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
            _review_conflict_records(conflict_groups, arbitrations),
        )
        debug.log_event(
            "review_round_finished",
            issue_count=len(final_issues),
            conflict_count=len(conflict_groups),
            unresolved_conflict_count=len(residual_conflicts),
            fallback_agent_count=fallback_agent_count,
        )
        return _ReviewRoundResult(
            issues=final_issues,
            pre_arbitration_issues=pre_arbitration_issues,
            arbitration_superseded=arbitration_superseded,
            conflict_groups=conflict_groups,
            residual_conflicts=residual_conflicts,
            fallback_agent_count=fallback_agent_count,
        )

    def propose_review_patches(
        self,
        round_result: _ReviewRoundResult,
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
            if self._runtime.config.pipeline.review_agent_loop and issue.get("agent_fallback"):
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
        style = self._runtime.analyzer.style_brief(analysis)
        book_synopsis = str(analysis.get("book_synopsis", "") or "")
        fixer = ReviewFixer(self._runtime.client, self._runtime.config)

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
            max(1, self._runtime.config.pipeline.review_concurrency),
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

    def run_session(
        self,
        store: RunStore,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """Repeat review, temporary revision and blind recheck on shadow text only.
        Formal chapters, manifest and glossary stay unchanged. Fixes update only the
        in-memory overlay and current review directory. Subsequent whole-book review
        receives revised shadow text without previous issue descriptions. Persist summaries,
        usage and formal events at session end.
        """
        manifest = store.load_manifest()
        self._runtime.flush_usage(store, scope="before_review")
        pending = [
            chapter["index"]
            for chapter in manifest.get("chapters", [])
            if chapter.get("status") != STATUS_DONE
        ]
        if pending:
            joined = ", ".join(str(index) for index in pending[:10])
            suffix = "…" if len(pending) > 10 else ""
            raise ValueError(
                f"Whole-book review requires every chapter to be translated; pending chapters: {joined}{suffix}"
            )

        chapter_rows = manifest.get("chapters", [])
        if progress:
            progress(0, len(chapter_rows), "Loading review chapters")
        loaded = []
        for position, item in enumerate(chapter_rows, start=1):
            loaded.append(store.load_chapter(item["index"]))
            if progress:
                progress(position, len(chapter_rows), "Loading review chapters")
        total = sum(len(chapter.text_segments) for chapter in loaded)
        if progress:
            progress(0, 0, "Restoring review checkpoint…")
        analysis = store.load_analysis() or {}
        reviewed_content_digest = _review_content_digest(loaded)

        # Reuse completed review results when content, configuration and glossary fingerprints match.
        latest_completed = store.load_latest_review_result()
        if (
            latest_completed is not None
            and latest_completed.get("status") == "completed"
            and latest_completed.get("reviewed_content_digest") == reviewed_content_digest
            and self._review_skip_eligible(store, latest_completed, all_terms)
        ):
            store.log_event("review_skipped", reason="already_completed")
            return ReviewOutcome(
                run_dir=os.path.join(
                    store.run_dir, "reviews", latest_completed.get("review_id", "")
                ),
                result=latest_completed,
                usage=self._review_usage_from_dir(store, latest_completed.get("review_id", "")),
            )

        # Resume incomplete review only when content, review configuration and glossary fingerprints match.
        debug = ReviewRunStore.find_resumable(
            store.run_dir,
            reviewed_content_digest,
            config=self._review_config_snapshot(),
            glossary_fingerprint=self._review_glossary_fingerprint(all_terms),
        )
        if debug is not None:
            from ..llm.usage import validate_usage

            validate_usage(debug.load_usage())
            debug.log_event("review_resumed_from_checkpoint", review_id=debug.review_id)
        else:
            debug = ReviewRunStore(store.run_dir)
        debug.start(
            reviewed_content_digest=reviewed_content_digest,
            metadata={
                "source_sha256": manifest.get("source_sha256"),
                "title": manifest.get("title"),
                "source_lang": self._runtime.config.source_lang,
                "target_lang": self._runtime.config.target_lang,
                "chapter_count": len(loaded),
                "total_segments": total,
                "config": self._review_config_snapshot(),
                "glossary_fingerprint": self._review_glossary_fingerprint(all_terms),
            },
        )
        store.log_event(
            "review_started",
            review_id=debug.review_id,
            review_dir=debug.run_dir,
            reviewed_content_digest=reviewed_content_digest,
        )

        def save_review_usage() -> dict[str, Any]:
            """Persist this review's usage delta and merge it into cumulative book usage."""
            from ..llm.usage import empty_usage

            self._runtime.flush_usage(store, scope="review", review=debug)
            return debug.load_usage() or empty_usage()

        target_overrides: dict[tuple[int, int], str] = {}
        seen_overlays = {_review_overlay_digest(loaded, target_overrides)}
        patch_records: list[dict[str, Any]] = []
        active_patches: dict[tuple[int, int], dict[str, Any]] = {}
        fix_failures: list[dict[str, Any]] = []
        blocked_issues: dict[str, dict[str, Any]] = {}
        round_summaries: list[dict[str, Any]] = []
        latest: _ReviewRoundResult | None = None
        clean_streak = 0
        fix_rounds = 0
        termination = "not_started"
        fix_loop = self._runtime.config.pipeline.review_fix_loop
        required_clean = self._runtime.config.pipeline.review_clean_confirmations if fix_loop else 1
        max_review_rounds = (
            (self._runtime.config.pipeline.review_fix_max_rounds + 1) * required_clean
            if fix_loop
            else 1
        )

        # Resume from the round checkpoint.
        _checkpoint = debug.load_checkpoint()
        _resume_scan_done = False
        _resume_latest: _ReviewRoundResult | None = None
        if _checkpoint is not None:
            start_round = _checkpoint.get("next_round", 1)
            # Tighter settings, such as fewer clean confirmations, may lower the round limit.
            # Clamp an out-of-range checkpoint to the final round instead of producing an empty loop.
            start_round = min(start_round, max_review_rounds)
            target_overrides = {
                (o["chapter"], o["index"]): o["target"]
                for o in _checkpoint.get("target_overrides", [])
            }
            seen_overlays = set(_checkpoint.get("seen_overlays", []))
            patch_records = _checkpoint.get("patch_records", [])
            active_patches = {
                (p["chapter"], p["index"]): p for p in _checkpoint.get("active_patches", [])
            }
            fix_failures = _checkpoint.get("fix_failures", [])
            blocked_issues = _checkpoint.get("blocked_issues", {})
            round_summaries = _checkpoint.get("round_summaries", [])
            clean_streak = _checkpoint.get("clean_streak", 0)
            fix_rounds = _checkpoint.get("fix_rounds", 0)
            # Restore the within-round phase; scan_done allows skipping the completed scan.
            if _checkpoint.get("phase") == "scan_done":
                _resume_scan_done = True
                start_round = _checkpoint.get("next_round", 1)
                # Do not reuse an old scan when a reduced limit is below the checkpoint's round number.
                if start_round > max_review_rounds:
                    _resume_scan_done = False
                    _resume_latest = None
                    start_round = max_review_rounds
                else:
                    _resume_latest = _ReviewRoundResult(
                        issues=_checkpoint.get("latest_issues", []),
                        pre_arbitration_issues=_checkpoint.get("latest_pre_arbitration_issues", []),
                        arbitration_superseded=_checkpoint.get("latest_arbitration_superseded", []),
                        conflict_groups=_checkpoint.get("latest_conflict_groups", []),
                        residual_conflicts=_checkpoint.get("latest_residual_conflicts", []),
                        fallback_agent_count=_checkpoint.get("latest_fallback_agent_count", 0),
                    )
                debug.log_event(
                    "review_checkpoint_restored",
                    next_round=start_round,
                    phase="scan_done",
                    override_count=len(target_overrides),
                    fix_rounds=fix_rounds,
                    clean_streak=clean_streak,
                )
            else:
                debug.log_event(
                    "review_checkpoint_restored",
                    next_round=start_round,
                    phase="round_done",
                    override_count=len(target_overrides),
                    fix_rounds=fix_rounds,
                    clean_streak=clean_streak,
                )
        else:
            start_round = 1

        def _save_checkpoint(
            current_round: int,
            phase: str = "round_done",
            latest: _ReviewRoundResult | None = None,
        ) -> None:
            """Save the round checkpoint with phase round_done or scan_done."""
            state: dict[str, Any] = {
                "phase": phase,
                "next_round": current_round + 1 if phase == "round_done" else current_round,
                "target_overrides": [
                    {"chapter": c, "index": i, "target": t}
                    for (c, i), t in sorted(target_overrides.items())
                ],
                "seen_overlays": sorted(seen_overlays),
                "patch_records": patch_records,
                "active_patches": [
                    {**p, "chapter": c, "index": i} for (c, i), p in sorted(active_patches.items())
                ],
                "fix_failures": fix_failures,
                "blocked_issues": blocked_issues,
                "round_summaries": round_summaries,
                "clean_streak": clean_streak,
                "fix_rounds": fix_rounds,
            }
            if latest is not None:
                state["latest_issues"] = latest.issues
                state["latest_pre_arbitration_issues"] = latest.pre_arbitration_issues
                state["latest_arbitration_superseded"] = latest.arbitration_superseded
                state["latest_conflict_groups"] = latest.conflict_groups
                state["latest_residual_conflicts"] = latest.residual_conflicts
                state["latest_fallback_agent_count"] = latest.fallback_agent_count
            debug.save_checkpoint(state)

        def register_blocked(
            issues: list[dict[str, Any]],
            failures: list[dict[str, Any]],
        ) -> None:
            """Retain fixer failures by stable issue key so later reviewer omissions cannot
            create false clean results.
            """
            by_id = {
                str(issue["issue_id"]): issue
                for issue in issues
                if isinstance(issue.get("issue_id"), str)
            }
            for failure in failures:
                failure_ids = failure.get("issue_ids")
                if not isinstance(failure_ids, list):
                    failure_id = failure.get("issue_id")
                    failure_ids = [failure_id] if isinstance(failure_id, str) else []
                for issue_id in failure_ids:
                    issue = by_id.get(str(issue_id))
                    if issue is None:
                        continue
                    issue_key = issue.get("issue_key")
                    if not isinstance(issue_key, str) or not issue_key:
                        continue
                    blocked_issues[issue_key] = {
                        **dict(issue),
                        "fix_failure": {
                            "status": failure.get("status"),
                            "reason": failure.get("reason"),
                            "review_round": failure.get("review_round"),
                        },
                    }

        def effective_issues(current: _ReviewRoundResult) -> list[dict[str, Any]]:
            """Merge current issues and historical unfixed issues into public unresolved issues
            in book order.
            """
            combined = {
                str(issue["issue_key"]): dict(issue)
                for issue in current.issues
                if isinstance(issue.get("issue_key"), str)
            }
            for issue_key, blocked in blocked_issues.items():
                current_issue = combined.get(issue_key)
                if current_issue is None:
                    combined[issue_key] = dict(blocked)
                    continue
                fix_failure = blocked.get("fix_failure")
                if isinstance(fix_failure, dict):
                    current_issue["fix_failure"] = dict(fix_failure)
            return sorted(
                combined.values(),
                key=lambda issue: (
                    issue.get("chapter", -1),
                    issue.get("index", -1),
                    issue.get("review_round", -1),
                    issue.get("issue_id", ""),
                ),
            )

        try:
            for review_round in range(start_round, max_review_rounds + 1):
                if progress:
                    progress(0, 0, f"Preparing review R{review_round}…")
                overlay_digest = _review_overlay_digest(loaded, target_overrides)
                evidence = BookEvidenceIndex(
                    loaded,
                    all_terms,
                    analysis,
                    target_overrides=target_overrides,
                )
                with debug.round_scope(review_round):
                    debug.log_event(
                        "review_round_started",
                        overlay_digest=overlay_digest,
                        override_count=len(target_overrides),
                    )
                    debug.write_json(
                        "overlay.json",
                        [
                            {
                                "chapter": chapter,
                                "index": index,
                                "target": target,
                            }
                            for (chapter, index), target in sorted(target_overrides.items())
                        ],
                    )
                    # When resuming scan_done, skip scanning and use cached results.
                    if (
                        _resume_scan_done
                        and review_round == start_round
                        and _resume_latest is not None
                    ):
                        latest = _resume_latest
                        _resume_scan_done = False
                        _resume_latest = None
                        # Rebuild initial/dismissed snapshots after skipping a scan so the final report remains complete.
                        debug.rebuild_snapshots_from_chunks(review_round)
                        debug.log_event("review_scan_skipped", review_round=review_round)
                    else:
                        latest = self.review_round(
                            loaded,
                            all_terms,
                            evidence,
                            debug,
                            review_round=review_round,
                            target_overrides=target_overrides,
                            progress=progress,
                        )
                        # Persist a mid-round checkpoint after scanning finishes.
                        _save_checkpoint(review_round, phase="scan_done", latest=latest)
                        # Persist scan usage before fixing so a fixer-stage crash cannot lose accounting.
                        save_review_usage()

                    current_issue_keys = {
                        str(issue["issue_key"])
                        for issue in latest.issues
                        if isinstance(issue.get("issue_key"), str)
                    }
                    for patch_record in active_patches.values():
                        if patch_record.get("round", review_round) >= review_round:
                            continue
                        covered_issue_keys = {
                            str(issue_key)
                            for issue_key in patch_record.get("issue_keys", [])
                            if isinstance(issue_key, str)
                        }
                        rereported = sorted(covered_issue_keys & current_issue_keys)
                        not_rereported = sorted(covered_issue_keys - current_issue_keys)
                        for issue_key in not_rereported:
                            blocked_issues.pop(issue_key, None)
                        patch_record["rereported_issue_keys"] = rereported
                        patch_record["not_rereported_issue_keys"] = not_rereported
                        if rereported:
                            patch_record["status"] = "needs_revision"
                            patch_record["failed_review_round"] = review_round
                        else:
                            if patch_record.get("status") != "not_rereported":
                                patch_record["not_rereported_in_round"] = review_round
                            patch_record["status"] = "not_rereported"

                    round_summary: dict[str, Any] = {
                        "review_round": review_round,
                        "overlay_digest": overlay_digest,
                        "override_count": len(target_overrides),
                        "issue_count": len(latest.issues),
                        "conflict_count": len(latest.conflict_groups),
                        "unresolved_conflict_count": len(latest.residual_conflicts),
                        "fallback_agent_count": latest.fallback_agent_count,
                        "clean_streak_before": clean_streak,
                        "blocked_issue_count": len(blocked_issues),
                    }
                    if not latest.issues:
                        if blocked_issues:
                            clean_streak = 0
                            if progress:
                                progress(0, required_clean, "Clean confirmation")
                            termination = "unresolved_fixes"
                            round_summary["clean_streak_after"] = 0
                            round_summary["patch_count"] = 0
                            round_summary["termination"] = termination
                            debug.write_json("summary.json", round_summary)
                            round_summaries.append(round_summary)
                            _save_checkpoint(review_round)
                            break
                        clean_streak += 1
                        if progress:
                            progress(clean_streak, required_clean, "Clean confirmation")
                        round_summary["clean_streak_after"] = clean_streak
                        round_summary["patch_count"] = 0
                        if clean_streak >= required_clean:
                            termination = "clean_confirmed"
                            round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        if termination == "clean_confirmed":
                            _save_checkpoint(review_round)
                            break
                        _save_checkpoint(review_round)
                        continue

                    if clean_streak and progress:
                        progress(0, required_clean, "Clean confirmation")
                    clean_streak = 0
                    round_summary["clean_streak_after"] = 0
                    if not fix_loop:
                        termination = "issues_reported"
                        round_summary["patch_count"] = 0
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break
                    if fix_rounds >= self._runtime.config.pipeline.review_fix_max_rounds:
                        termination = "max_rounds"
                        round_summary["patch_count"] = 0
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break

                    patches, failures = self.propose_review_patches(
                        latest,
                        evidence,
                        all_terms,
                        analysis,
                        debug,
                        review_round=review_round,
                        fix_round=fix_rounds + 1,
                        progress=progress,
                    )
                    fix_failures.extend(
                        [
                            {
                                **failure,
                                "review_round": review_round,
                            }
                            for failure in failures
                        ]
                    )
                    register_blocked(
                        latest.issues,
                        [
                            {
                                **failure,
                                "review_round": review_round,
                            }
                            for failure in failures
                        ],
                    )
                    round_summary["patch_count"] = len(patches)
                    if not patches:
                        termination = "no_progress"
                        round_summary["fix_failure_count"] = len(failures)
                        round_summary["blocked_issue_count"] = len(blocked_issues)
                        round_summary["termination"] = termination
                        debug.write_json("patches.json", [])
                        debug.write_json("fix_failures.json", failures)
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break

                    issue_keys_by_id = {
                        str(issue["issue_id"]): str(issue["issue_key"])
                        for issue in latest.issues
                        if isinstance(issue.get("issue_id"), str)
                        and isinstance(issue.get("issue_key"), str)
                    }
                    candidate_overrides = dict(target_overrides)
                    applicable: list[ProvisionalPatch] = []
                    hash_failures: list[dict[str, Any]] = []
                    for patch in patches:
                        location = (patch.chapter, patch.index)
                        current = evidence.segment_ref(*location)
                        if (
                            current is None
                            or ReviewFixer.target_hash(current.target) != patch.before_hash
                        ):
                            failure = {
                                "patch_id": patch.patch_id,
                                "issue_ids": list(patch.issue_ids),
                                "chapter": patch.chapter,
                                "index": patch.index,
                                "status": "failed",
                                "reason": "before_hash_changed",
                                "review_round": review_round,
                            }
                            fix_failures.append(failure)
                            failures.append(failure)
                            hash_failures.append(failure)
                            continue
                        candidate_overrides[location] = patch.after
                        applicable.append(patch)

                    register_blocked(
                        latest.issues,
                        hash_failures,
                    )
                    round_summary["fix_failure_count"] = len(failures)
                    round_summary["blocked_issue_count"] = len(blocked_issues)
                    candidate_digest = _review_overlay_digest(
                        loaded,
                        candidate_overrides,
                    )
                    debug.write_json(
                        "patches.json",
                        [patch.as_dict() for patch in patches],
                    )
                    debug.write_json("fix_failures.json", failures)
                    round_summary["candidate_overlay_digest"] = candidate_digest
                    round_summary["applicable_patch_count"] = len(applicable)
                    if not applicable or candidate_digest == overlay_digest:
                        termination = "no_progress"
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break
                    if candidate_digest in seen_overlays:
                        termination = "cycle_detected"
                        for patch in applicable:
                            record = {
                                **patch.as_dict(),
                                "issue_keys": sorted(
                                    {
                                        issue_keys_by_id[issue_id]
                                        for issue_id in patch.issue_ids
                                        if issue_id in issue_keys_by_id
                                    }
                                ),
                                "status": "rejected_cycle",
                            }
                            patch_records.append(record)
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break

                    fix_rounds += 1
                    for patch in applicable:
                        location = (patch.chapter, patch.index)
                        previous = active_patches.get(location)
                        record = patch.as_dict()
                        record["issue_keys"] = sorted(
                            {
                                issue_keys_by_id[issue_id]
                                for issue_id in patch.issue_ids
                                if issue_id in issue_keys_by_id
                            }
                        )
                        if previous is not None:
                            previous["status"] = "superseded"
                            previous["superseded_by"] = patch.patch_id
                        patch_records.append(record)
                        active_patches[location] = record
                    target_overrides = candidate_overrides
                    seen_overlays.add(candidate_digest)
                    round_summary["fix_round"] = fix_rounds
                    debug.write_json("summary.json", round_summary)
                    round_summaries.append(round_summary)
                    _save_checkpoint(review_round)
            else:
                termination = "max_rounds"
                _save_checkpoint(max_review_rounds)

            if latest is None:  # pragma: no cover - max_review_rounds is at least one.
                raise RuntimeError("Review loop finished without a review round")

            unresolved = effective_issues(latest)
            final_conflicts = _review_unresolved_conflict_records(unresolved)
            final_residual_conflicts = [
                record
                for record in final_conflicts
                if record.get("arbitration", {}).get("status") == "unresolved"
            ]
            final_fallback_agent_count = _review_unresolved_fallback_count(unresolved)
            initial_issues, dismissed = debug.result_snapshots()
            debug.write_json("rounds/final/initial_issues.json", initial_issues)
            debug.write_json("rounds/final/dismissed_issues.json", dismissed)
            debug.write_json(
                "rounds/final/pre_arbitration_issues.json",
                latest.pre_arbitration_issues,
            )
            debug.write_json(
                "rounds/final/arbitration_superseded_issues.json",
                latest.arbitration_superseded,
            )
            debug.write_json(
                "rounds/final/residual_conflicts.json",
                [
                    {
                        "conflict_id": record["conflict_id"],
                        "consistency_key": record["consistency_key"],
                        "issue_ids": record["issue_ids"],
                    }
                    for record in final_residual_conflicts
                ],
            )
            debug.write_json("rounds/final/conflicts.json", final_conflicts)
            debug.write_json("rounds/final/patch-history.json", patch_records)
            debug.write_json(
                "rounds/final/not_rereported_patches.json",
                [patch for patch in patch_records if patch["status"] == "not_rereported"],
            )
            debug.write_json(
                "rounds/final/unresolved_issues.json",
                unresolved,
            )
            debug.write_json("rounds/final/fix_failures.json", fix_failures)
            debug.write_json("rounds/final/rounds.json", round_summaries)
            debug.write_json(
                "rounds/final/shadow_targets.json",
                [
                    {
                        "chapter": chapter,
                        "index": index,
                        "target": target,
                    }
                    for (chapter, index), target in sorted(target_overrides.items())
                ],
            )
            public_issues = _review_public_issues(unresolved)
            changes = _review_net_changes(
                loaded,
                target_overrides,
                patch_records,
                active_patches,
            )
            summary = {
                "initial_issue_count": len(initial_issues),
                "dismissed_issue_count": len(dismissed),
                "pre_arbitration_issue_count": len(latest.pre_arbitration_issues),
                "arbitration_superseded_count": len(latest.arbitration_superseded),
                "issue_count": len(public_issues),
                "conflict_count": len(final_conflicts),
                "unresolved_conflict_count": len(final_residual_conflicts),
                "fallback_agent_count": final_fallback_agent_count,
                "review_round_count": len(round_summaries),
                "fix_round_count": fix_rounds,
                "patch_count": len(patch_records),
                "change_count": len(changes),
                "not_rereported_patch_count": sum(
                    patch["status"] == "not_rereported" for patch in patch_records
                ),
                "shadow_override_count": len(target_overrides),
                "blocked_issue_count": len(blocked_issues),
                "clean_streak": clean_streak,
            }
            debug.write_json("rounds/final/summary.json", summary)
            result = debug.finish(
                status="completed",
                termination=termination,
                summary=summary,
                issues=public_issues,
                changes=changes,
            )
            usage = save_review_usage()
            store.log_event(
                "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="completed",
                termination=termination,
                issue_count=len(public_issues),
                change_count=len(changes),
            )
            return ReviewOutcome(
                run_dir=debug.run_dir,
                result=result,
                usage=usage,
            )
        except BaseException as error:
            resumable_interrupt = not isinstance(
                error, Exception
            ) or is_resumable_provider_interrupt(error)
            if not isinstance(error, Exception):
                save_review_usage()
                debug.mark_interrupted(error={"type": type(error).__name__, "message": str(error)})
                store.log_event(
                    "review_interrupted",
                    review_id=debug.review_id,
                    review_dir=debug.run_dir,
                    status="interrupted",
                    error_type=type(error).__name__,
                )
                raise
            initial_issues, dismissed = debug.result_snapshots()
            partial_issues = effective_issues(latest) if latest is not None else []
            public_issues = _review_public_issues(partial_issues)
            partial_changes = _review_net_changes(
                loaded,
                target_overrides,
                patch_records,
                active_patches,
            )
            summary = {
                "issue_count": len(public_issues),
                "change_count": len(partial_changes),
                "conflict_count": (len(latest.conflict_groups) if latest is not None else 0),
                "fallback_agent_count": (latest.fallback_agent_count if latest is not None else 0),
            }
            error_payload = {"type": type(error).__name__, "message": str(error)}
            debug.write_json("rounds/final/initial_issues.json", initial_issues)
            debug.write_json("rounds/final/dismissed_issues.json", dismissed)
            debug.write_json(
                "rounds/final/partial_issues.json",
                partial_issues,
            )
            debug.write_json("rounds/final/partial_patches.json", patch_records)
            debug.write_json("rounds/final/fix_failures.json", fix_failures)
            if resumable_interrupt:
                # Keep chunk/checkpoint caches eligible for find_resumable after balance,
                # timeout or transport stops. Formal chapters remain unchanged until Autofix.
                debug.mark_interrupted(
                    error=error_payload,
                    summary=summary,
                    issues=public_issues,
                    changes=partial_changes,
                )
                save_review_usage()
                store.log_event(
                    "review_interrupted",
                    review_id=debug.review_id,
                    review_dir=debug.run_dir,
                    status="interrupted",
                    issue_count=len(public_issues),
                    change_count=len(partial_changes),
                    error_type=type(error).__name__,
                    error=str(error),
                )
                raise
            debug.finish(
                status="failed",
                termination="error",
                summary=summary,
                issues=public_issues,
                changes=partial_changes,
                error=error_payload,
            )
            save_review_usage()
            store.log_event(
                "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="failed",
                termination="error",
                issue_count=len(public_issues),
                change_count=len(partial_changes),
                error_type=type(error).__name__,
                error=str(error),
            )
            raise

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
        budget = self._runtime.config.segment.max_chars_per_batch * 3
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
            if self._runtime.config.pipeline.glossary_scope != "chapter":
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
                cached_sub = self._try_cached_subchunks(
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
                    review_result = self._runtime.reviewer.review_result(
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
                and self._runtime.config.pipeline.review_agent_loop
                and chapter_index is not None
            ):
                outcome = ReviewAgentLoop(
                    self._runtime.client,
                    self._runtime.config,
                    evidence,
                    debug,
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
                retries = self._runtime.config.pipeline.review_output_retries
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
            max(1, self._runtime.config.pipeline.review_concurrency),
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
    def _try_cached_subchunks(
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
        """Pack paragraphs into contiguous blocks by source-character budget without changing
        order.
        """
        chunks: list[list] = []
        cur: list = []
        size = 0
        for s in segs:
            if cur and size + len(s.source) > budget:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(s)
            size += len(s.source)
        if cur:
            chunks.append(cur)
        return chunks
