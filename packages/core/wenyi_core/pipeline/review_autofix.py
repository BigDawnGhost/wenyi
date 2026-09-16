"""Review Autofix publication service.
The review engine stays read-only. After its results are persisted, overlay changes on an
immutable working snapshot and verify remaining issues by paragraph through the bounded
evidence loop. Prepare every candidate and persist autofix/index.json before modifying
formal chapter target text, enabling idempotent recovery through before/after hashes.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..glossary.store import GlossaryTerm
from ..llm.usage import validate_usage
from ..review.models import ReviewOutcome
from ..review.run_store import ReviewRunStore
from ..storage.protocol import Storage
from .autofix_candidates import AutofixCandidateService
from .autofix_plan import prepare_identity, save_plan
from .autofix_publish import AutofixPublisher
from .docx_styles import DocxStyleService

if TYPE_CHECKING:
    from .annotations import AnnotationService
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


class ReviewAutofixService:
    """Publish read-only review recommendations to formal chapters with a recoverable index."""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._publisher = AutofixPublisher(annotations, DocxStyleService(runtime))
        self._candidates = AutofixCandidateService(
            runtime.config, runtime.client, runtime.analyzer.style_brief
        )

    @staticmethod
    def _review_result(debug: ReviewRunStore) -> dict[str, Any] | None:
        result = debug.load_json("result.json")
        return result if isinstance(result, dict) else None

    @staticmethod
    def _index(debug: ReviewRunStore) -> dict[str, Any] | None:
        result = debug.load_json("autofix/index.json")
        return result if isinstance(result, dict) else None

    def resume_pending(
        self,
        store: Storage,
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome | None:
        """Finish interrupted indexed publication before repeating review or agent calls."""
        for name in ReviewRunStore.list_review_ids(store):
            if not name.startswith("review-"):
                continue
            run_dir = os.path.join(store.reviews_dir, name)
            debug = ReviewRunStore.open_existing(run_dir, storage=store)
            result = self._review_result(debug)
            if result is None:
                continue
            index = self._index(debug)
            # Handle only the newest valid review. If it lacks an index, the normal path reuses its result
            # and plans autofix; never bypass it to publish an older leftover index.
            if index is None:
                return None
            status = index.get("status")
            result_autofix = result.get("autofix")
            if status == "applying":
                debug = ReviewRunStore.open_existing(run_dir, storage=store)
                debug.log_event("review_autofix_resumed", review_id=name)
                return self._apply_index(store, debug, index, result, progress=progress)
            if status in {"completed", "partial"} and not isinstance(result_autofix, dict):
                debug = ReviewRunStore.open_existing(run_dir, storage=store)
                return self._publisher.finish(store, debug, index, result)
            # Newest directories come first; once the newest is complete, never publish earlier indices.
            if status in {"completed", "partial"}:
                return None
        return None

    def run(
        self,
        store: Storage,
        outcome: ReviewOutcome,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """Flush completed model responses even if planning or publication is interrupted."""
        if not self._runtime.config.pipeline.review_autofix:
            return outcome
        debug = ReviewRunStore.open_existing(outcome.run_dir, storage=store)
        try:
            return self._run(store, outcome, all_terms, progress=progress)
        finally:
            self._save_usage_delta(store, debug, scope="review_autofix")

    def _run(
        self,
        store: Storage,
        outcome: ReviewOutcome,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """Prepare final candidates and an index for a completed review, then publish
        idempotently.
        """
        if not self._runtime.config.pipeline.review_autofix:
            return outcome
        debug = ReviewRunStore.open_existing(outcome.run_dir, storage=store)
        validate_usage(debug.load_usage())
        existing = self._index(debug)
        if existing is not None:
            status = existing.get("status")
            if status == "applying":
                return self._apply_index(
                    store,
                    debug,
                    existing,
                    outcome.result,
                    progress=progress,
                )
            if status in {"completed", "partial"}:
                return self._publisher.finish(store, debug, existing, outcome.result)

        inference = prepare_identity(debug, self._runtime.llm_config)
        manifest = store.load_manifest()
        chapters = [
            store.load_chapter(row["index"])
            for row in manifest.get("chapters", [])
            if isinstance(row.get("index"), int)
        ]
        candidates = self._candidates.prepare(
            chapters, store.load_analysis() or {}, outcome, all_terms, debug, progress=progress
        )
        index = dict(save_plan(chapters, candidates, inference, debug, outcome))
        self._save_usage_delta(store, debug, scope="review_autofix_agent")
        return self._apply_index(
            store,
            debug,
            index,
            outcome.result,
            progress=progress,
        )

    def _save_usage_delta(
        self,
        store: Storage,
        debug: ReviewRunStore,
        *,
        scope: str,
    ) -> None:
        """Merge unflushed runtime calls into both the review ledger and whole-book totals."""
        self._runtime.flush_usage(store, scope=scope, review=debug)

    def _apply_index(
        self,
        store: Storage,
        debug: ReviewRunStore,
        index: dict[str, Any],
        result: dict[str, Any],
        *,
        progress: ProgressFn | None,
    ) -> ReviewOutcome:
        self._publisher.apply(store, debug, index, result, progress=progress)
        self._save_usage_delta(store, debug, scope="review_autofix_publish")
        return self._publisher.finish(store, debug, index, result)
