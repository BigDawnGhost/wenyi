"""The public orchestration facade for workflow control.
Assemble runtime and preparation, translation, annotation, review, autofix and finalization
services. Route steps, order stages, choose lock scopes, forward
progress and propagate exceptions with consistent return structures.
All parsing, model calls, state I/O, pools, glossary operations, alignment, review state
machines, reports, exports and accounting belong to domain services. This facade must not
directly depend on agents, ingest, glossary, assemble or ThreadPoolExecutor, nor read/write
state files.
Dependencies flow from CLI to Orchestrator, then Runtime/domain services, then
agents/ingest/glossary/assemble/RunStore. Lower layers must never import this module.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import Config
from .annotations import AnnotationService
from .finalization import AssemblyService, ReportService
from .preparation import PreparationService
from .review_autofix import ReviewAutofixService
from .review_workflow import ReviewService
from .runstore import RunStore
from .runtime import LLMClient, PipelineRuntime
from .translation import TranslationService

ProgressFn = Callable[[int, int, str], None]


class Orchestrator:
    """Assemble runtime/services and control only step routing and lock scopes."""

    # Optional stages and the complete workflow.
    ALL_STEPS = ("translate", "review", "report", "assemble")

    def __init__(self, config: Config, client: LLMClient | None = None):
        """Assemble shared runtime and domain services without domain I/O."""
        self.config = config
        self._runtime = PipelineRuntime(config, client=client)
        self.client = self._runtime.client
        self._preparation = PreparationService(self._runtime)
        self._annotations = AnnotationService(self._runtime)
        self._translation = TranslationService(self._runtime, self._annotations)
        self._review = ReviewService(self._runtime)
        self._review_autofix = ReviewAutofixService(self._runtime, self._annotations)
        self._report = ReportService(self._runtime)
        self._assembly = AssemblyService(self._runtime)

    # Public entry points.
    def prepare(self, input_path: str, *, progress: ProgressFn | None = None) -> RunStore:
        """Parse input and locate state; initialize first runs under the book lock."""
        with self._runtime.track_workflow("prepare"):
            return self._preparation.prepare(input_path, progress=progress)

    def prepare_for_translation(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """Complete all preparation without translating body text.
        Parse the document, detect language, analyze style and initial terms, and optionally
        prescan chapters and synthesize a synopsis. Every stage resumes by reusing persisted
        results.
        """
        with self._runtime.track_workflow("prepare"):
            store = self._preparation.prepare(input_path, progress=progress)
            with store.lock():
                self._preparation.activate(store)
                try:
                    self._preparation.ensure_understanding(store, progress=progress)
                    self._runtime.log_event(
                        store,
                        "translation_prepared",
                        input_path=input_path,
                        book_understanding=self.config.pipeline.book_understanding,
                    )
                finally:
                    self._runtime.flush_usage(store, scope="prepare")
            return store

    def run(
        self,
        input_path: str,
        *,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """Prepare state and translate pending chapters under the book lock."""
        with self._runtime.track_workflow("translate"):
            store = self._preparation.prepare(input_path, progress=progress)
            with store.lock():
                return self._run_locked(
                    store,
                    only_chapter=only_chapter,
                    progress=progress,
                )

    def _run_locked(
        self,
        store: RunStore,
        *,
        only_chapter: int | None,
        progress: ProgressFn | None,
    ) -> RunStore:
        """Restore languages, validate chapter selection, build the synopsis and delegate
        translation.
        """
        manifest = self._preparation.activate(store)
        chapter_indices = {chapter.get("index") for chapter in manifest.get("chapters", [])}
        if only_chapter is not None and only_chapter not in chapter_indices:
            available = sorted(index for index in chapter_indices if isinstance(index, int))
            valid_range = f"0–{available[-1]}" if available else "no translatable chapters"
            raise ValueError(
                f"Chapter index {only_chapter} does not exist; available range: {valid_range}"
            )
        book_synopsis = self._preparation.ensure_understanding(store, progress=progress)
        return self._translation.run(
            store,
            book_synopsis=book_synopsis,
            only_chapter=only_chapter,
            progress=progress,
        )

    def run_review(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """Run complete review and publish autofix results when configured."""
        with self._runtime.track_workflow("review"):
            store = self._preparation.locate_existing(input_path, progress=progress)
            with store.lock():
                self._preparation.activate(store)
                terms = self._review.session_terms(store)
                outcome = self._run_review_locked(
                    store,
                    terms,
                    progress=progress,
                )
            return {
                "store": store,
                "review_issues": outcome.issues,
                "review_changes": outcome.changes,
                "review_result": outcome.result,
                "review_dir": outcome.run_dir,
            }

    def _run_review_locked(
        self,
        store: RunStore,
        terms: Any,
        *,
        progress: ProgressFn | None,
    ) -> Any:
        """Run read-only review under the book lock, then enter the separate optional autofix
        publisher.
        """
        resumed = self._review_autofix.resume_pending(store, progress=progress)
        if resumed is not None:
            return resumed
        outcome = self._review.run_session(store, terms, progress=progress)
        if not self.config.pipeline.review_autofix:
            return outcome
        return self._review_autofix.run(store, outcome, terms, progress=progress)

    def _run_existing_steps(
        self,
        input_path: str,
        steps: set[str],
        *,
        progress: ProgressFn | None,
        out_format: str | None = None,
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """Run local finalization from existing state without creating a translation task."""
        store = self._preparation.locate_existing(input_path, progress=progress)
        with store.lock():
            self._preparation.activate(store)
            return self._finish_steps_locked(
                store,
                input_path=input_path,
                steps=steps,
                run_steps_input=sorted(steps),
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )

    def run_report(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """Regenerate the report from existing state."""
        return self._run_existing_steps(
            input_path,
            {"report"},
            progress=progress,
        )

    def run_assemble(
        self,
        input_path: str,
        *,
        out_format: str | None = None,
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """Export an existing-state snapshot without waiting for ongoing whole-book
        translation.
        """
        with self._runtime.track_workflow("assemble"):
            store = self._preparation.locate_existing(input_path, progress=progress)
            self._runtime.log_event(
                store,
                "run_steps_started",
                steps=["assemble"],
                input_path=input_path,
            )
            outputs = self._assembly.assemble_snapshot(
                store,
                input_path=input_path,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            self._runtime.log_event(
                store, "run_steps_finished", steps=["assemble"], outputs=outputs
            )
            return {
                "store": store,
                "output": outputs[0] if outputs else None,
                "outputs": outputs,
                "report": None,
                "review_issues": [],
                "review_changes": [],
                "review_result": None,
                "review_dir": None,
            }

    def run_steps(
        self,
        input_path: str,
        steps,
        *,
        progress: ProgressFn | None = None,
        out_format: str | None = None,
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """Run any requested subset of ALL_STEPS."""
        with self._runtime.track_workflow("workflow"):
            steps = set(steps)
            run_steps_input = sorted(steps)
            if steps == {"review"}:
                reviewed = self.run_review(input_path, progress=progress)
                return {
                    "store": reviewed["store"],
                    "output": None,
                    "outputs": [],
                    "report": None,
                    "review_issues": reviewed["review_issues"],
                    "review_changes": reviewed["review_changes"],
                    "review_result": reviewed["review_result"],
                    "review_dir": reviewed["review_dir"],
                }
            if steps == {"assemble"}:
                return self.run_assemble(
                    input_path,
                    out_format=out_format,
                    out_path=out_path,
                    pdf_engine=pdf_engine,
                    progress=progress,
                )

            if "translate" in steps:
                store = self.run(input_path, progress=progress)
            else:
                store = self._preparation.prepare(input_path, progress=progress)
                self._preparation.activate(store)
            with store.lock():
                return self._finish_steps_locked(
                    store,
                    input_path=input_path,
                    steps=steps,
                    run_steps_input=run_steps_input,
                    progress=progress,
                    out_format=out_format,
                    out_path=out_path,
                    pdf_engine=pdf_engine,
                )

    def _finish_steps_locked(
        self,
        store: RunStore,
        *,
        input_path: str,
        steps: set[str],
        run_steps_input: list[str],
        progress: ProgressFn | None,
        out_format: str | None,
        out_path: str | None,
        pdf_engine: str,
    ) -> dict[str, Any]:
        """Delegate review, reporting and assembly under the book lock and return combined
        results.
        """
        self._runtime.log_event(
            store,
            "run_steps_started",
            steps=run_steps_input,
            input_path=input_path,
        )

        review_issues: list[dict] = []
        review_changes: list[dict] = []
        review_result: dict[str, Any] | None = None
        review_dir: str | None = None
        report: dict[str, Any] | None = None
        with self._report.glossary_scope(store, "report" in steps) as glossary:
            try:
                if "review" in steps:
                    # Flush earlier stage deltas first so session usage.json contains only review calls.
                    self._runtime.flush_usage(store, scope="pipeline")
                    terms = self._review.session_terms(store, glossary)
                    outcome = self._run_review_locked(
                        store,
                        terms,
                        progress=progress,
                    )
                    review_issues = outcome.issues
                    review_changes = outcome.changes
                    review_result = outcome.result
                    review_dir = outcome.run_dir

                self._runtime.flush_usage(store, scope="pipeline")
                if "report" in steps:
                    if glossary is None:  # pragma: no cover - Guaranteed by the needs condition.
                        raise RuntimeError("Report generation requires a glossary")
                    report = self._report.build_and_save(
                        store,
                        glossary,
                        progress=progress,
                    )
            finally:
                self._runtime.flush_usage(store, scope="pipeline")

        outputs: list[str] = []
        if "assemble" in steps:
            outputs = self._assembly.assemble_live(
                store,
                input_path=input_path,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )

        self._runtime.log_event(
            store,
            "run_steps_finished",
            steps=run_steps_input,
            outputs=outputs,
        )
        return {
            "store": store,
            "output": outputs[0] if outputs else None,
            "outputs": outputs,
            "report": report,
            "review_issues": review_issues,
            "review_changes": review_changes,
            "review_result": review_result,
            "review_dir": review_dir,
        }

    def run_all(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
        out_format: str | None = None,
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """Translate, review, report and assemble, returning combined results."""
        steps = {"translate", "report", "assemble"}
        if self.config.pipeline.review:
            steps.add("review")
        return self.run_steps(
            input_path,
            steps,
            progress=progress,
            out_format=out_format,
            out_path=out_path,
            pdf_engine=pdf_engine,
        )
