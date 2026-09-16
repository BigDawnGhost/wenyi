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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..glossary.store import GlossaryStore, GlossaryTerm
from ..i18n.resources import prompt_fingerprint
from ..llm.retrying import is_resumable_provider_interrupt
from ..review.evidence import BookEvidenceIndex
from ..review.models import ReviewOutcome
from ..review.run_store import ReviewRunStore
from ..review.session import ReviewPolicy, content_digest, review_overlay_digest
from ..storage.protocol import Storage
from . import review_results
from .review_checkpoint import ReviewCheckpoint, ReviewInputs
from .review_chunks import ReviewChunkService
from .review_rounds import ReviewRoundService
from .runstore import STATUS_DONE

if TYPE_CHECKING:
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


class ReviewService:
    """Domain service for read-only whole-book agent review."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime
        self._chunks = ReviewChunkService(runtime.config, runtime.client, runtime.reviewer)
        self._rounds = ReviewRoundService(
            runtime.config, runtime.client, runtime.analyzer.style_brief, self._chunks
        )

    def session_terms(
        self,
        store: Storage,
        glossary: Storage | GlossaryStore | GlossaryStore | None = None,
    ) -> list[GlossaryTerm]:
        """Return the final glossary snapshot used by this review."""
        if glossary is not None:
            return glossary.all_terms()
        return store.all_terms()

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
        store: Storage,
        latest: dict[str, Any],
        terms: list[GlossaryTerm],
    ) -> bool:
        """Reuse a completed review only when content, configuration and glossary all match."""
        review_id = latest.get("review_id")
        if not isinstance(review_id, str) or not review_id:
            return False
        metadata = store.read_artifact(f"reviews/{review_id}/rounds/metadata.json")
        if not isinstance(metadata, dict):
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
    def _review_usage_from_dir(store: Storage, review_id: str) -> dict[str, Any]:
        """Read usage from a completed review directory; return empty when unavailable."""
        usage = store.read_artifact(f"reviews/{review_id}/usage.json")
        return usage if isinstance(usage, dict) else {}

    def _open_session(
        self, store: Storage, all_terms: list[GlossaryTerm], progress: ProgressFn | None
    ) -> ReviewInputs | ReviewOutcome:
        """Validate formal state and restore or initialize the appropriate Review directory."""
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
        reviewed_content_digest = content_digest(loaded)

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
            storage=store,
        )
        if debug is not None:
            from ..llm.usage import validate_usage

            validate_usage(debug.load_usage())
            debug.log_event("review_resumed_from_checkpoint", review_id=debug.review_id)
        else:
            debug = ReviewRunStore(store.run_dir, storage=store)
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

        return ReviewInputs(loaded, analysis, debug)

    def run_session(
        self,
        store: Storage,
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
        opened = self._open_session(store, all_terms, progress)
        if isinstance(opened, ReviewOutcome):
            return opened
        loaded, analysis, debug = opened.chapters, opened.analysis, opened.debug

        def save_review_usage() -> dict[str, Any]:
            """Persist this review's usage delta and merge it into cumulative book usage."""
            from ..llm.usage import empty_usage

            self._runtime.flush_usage(store, scope="review", review=debug)
            return debug.load_usage() or empty_usage()

        policy = ReviewPolicy(
            self._runtime.config.pipeline.review_fix_loop,
            self._runtime.config.pipeline.review_fix_max_rounds,
            self._runtime.config.pipeline.review_clean_confirmations,
        )
        max_review_rounds = policy.max_review_rounds

        checkpoint = ReviewCheckpoint(debug)
        recovery = checkpoint.restore(review_overlay_digest(loaded, {}), max_review_rounds)
        state = recovery.state
        start_round = recovery.start_round
        _resume_latest = recovery.latest
        _resume_scan_done = _resume_latest is not None

        try:
            for review_round in range(start_round, max_review_rounds + 1):
                if progress:
                    progress(0, 0, f"Preparing review R{review_round}…")
                overlay_digest = review_overlay_digest(loaded, state.target_overrides)
                evidence = BookEvidenceIndex(
                    loaded,
                    all_terms,
                    analysis,
                    target_overrides=state.target_overrides,
                )
                with debug.round_scope(review_round):
                    debug.log_event(
                        "review_round_started",
                        overlay_digest=overlay_digest,
                        override_count=len(state.target_overrides),
                    )
                    debug.write_json(
                        "overlay.json",
                        [
                            {
                                "chapter": chapter,
                                "index": index,
                                "target": target,
                            }
                            for (chapter, index), target in sorted(state.target_overrides.items())
                        ],
                    )
                    # When resuming scan_done, skip scanning and use cached results.
                    if (
                        _resume_scan_done
                        and review_round == start_round
                        and _resume_latest is not None
                    ):
                        state.latest = _resume_latest
                        _resume_scan_done = False
                        _resume_latest = None
                        # Rebuild initial/dismissed snapshots after skipping a scan so the final report remains complete.
                        debug.rebuild_snapshots_from_chunks(review_round)
                        debug.log_event("review_scan_skipped", review_round=review_round)
                    else:
                        state.latest = self._rounds.review_round(
                            loaded,
                            all_terms,
                            evidence,
                            debug,
                            review_round=review_round,
                            target_overrides=state.target_overrides,
                            progress=progress,
                        )
                        # Persist a mid-round checkpoint after scanning finishes.
                        checkpoint.save(state, review_round, phase="scan_done", latest=state.latest)
                        # Persist scan usage before fixing so a fixer-stage crash cannot lose accounting.
                        save_review_usage()

                    decision = state.after_scan(state.latest, review_round, overlay_digest, policy)
                    round_summary = decision.summary
                    if progress and decision.clean_progress is not None:
                        progress(
                            decision.clean_progress, policy.required_clean, "Clean confirmation"
                        )
                    if decision.action != "fix":
                        debug.write_json("summary.json", round_summary)
                        state.round_summaries.append(round_summary)
                        checkpoint.save(state, review_round)
                        if decision.action == "stop":
                            break
                        continue

                    patches, failures = self._rounds.propose_review_patches(
                        state.latest,
                        evidence,
                        all_terms,
                        analysis,
                        debug,
                        review_round=review_round,
                        fix_round=state.fix_rounds + 1,
                        progress=progress,
                    )
                    current_targets = {
                        (patch.chapter, patch.index): segment.target
                        for patch in patches
                        if (segment := evidence.segment_ref(patch.chapter, patch.index)) is not None
                    }
                    plan = state.prepare_fix(
                        loaded,
                        state.latest,
                        [patch.as_dict() for patch in patches],
                        failures,
                        current_targets,
                        review_round,
                        round_summary,
                    )
                    debug.write_json("patches.json", [patch.as_dict() for patch in patches])
                    debug.write_json("fix_failures.json", failures)
                    advanced = False
                    if plan is not None:
                        round_summary["candidate_overlay_digest"] = plan.candidate_digest
                        round_summary["applicable_patch_count"] = len(plan.applicable)
                        advanced = state.after_fix(plan, overlay_digest, round_summary)
                    debug.write_json("summary.json", round_summary)
                    state.round_summaries.append(round_summary)
                    checkpoint.save(state, review_round)
                    if not advanced:
                        break

            else:
                state.termination = "max_rounds"
                checkpoint.save(state, max_review_rounds)

            result = review_results.write_completed(debug, state, loaded)
            usage = save_review_usage()
            store.log_event(
                "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="completed",
                termination=state.termination,
                issue_count=len(result["issues"]),
                change_count=len(result["changes"]),
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
            public_issues, partial_changes = review_results.write_partial(
                debug,
                state,
                loaded,
                error,
                resumable_interrupt,
            )
            save_review_usage()
            store.log_event(
                "review_interrupted" if resumable_interrupt else "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="interrupted" if resumable_interrupt else "failed",
                **({} if resumable_interrupt else {"termination": "error"}),
                issue_count=len(public_issues),
                change_count=len(partial_changes),
                error_type=type(error).__name__,
                error=str(error),
            )
            raise
