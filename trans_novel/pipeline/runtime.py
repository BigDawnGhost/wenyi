"""Shared runtime owning Config, LLMClient and every agent for one Orchestrator.
This is neither a global singleton nor a guarantee of concurrent reuse across books.
Centralize construction to avoid duplicate clients/accounting or lost language state.
Restore manifest languages into config and all agents.
Own event sinks, usage checkpoints/flushes, metrics sessions, stage timing, snapshots and
source hashes. Preserve existing fallback behavior for accounting and events; metrics
failures may warn but must not change workflow results.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from typing import Any

from ..agents.analyzer import Analyzer
from ..agents.annotation_aligner import AnnotationAligner
from ..agents.polisher import Polisher
from ..agents.reviewer import Reviewer
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from .language import require_language, validate_run_languages
from .metrics import RunMetricsRecorder
from .runstore import RunStore, source_sha256

ProgressFn = Callable[[int, int, str], None]

# Per-run ledgers under run_metrics/ remain disabled until the feature is ready.
_RUN_METRICS_ENABLED = False


def _record_run_metrics(
    operation: str,
    requested_steps: list[str],
    *,
    invocation_fields: tuple[str, ...] = (),
) -> Callable:
    """Add per-run metrics to a fixed entry point while safely supporting nested entries."""

    def decorator(func: Callable) -> Callable:
        call_signature = signature(func)

        @wraps(func)
        def wrapped(
            self,
            input_path: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            bound = call_signature.bind(self, input_path, *args, **kwargs)
            bound.apply_defaults()
            invocation = {name: bound.arguments.get(name) for name in invocation_fields}
            # The decorator lives on facade methods, but Runtime owns the ledger, avoiding reverse dependencies.
            with self._runtime.run_metrics_session(
                input_path,
                operation=operation,
                requested_steps=requested_steps,
                invocation=invocation,
            ):
                return func(self, input_path, *args, **kwargs)

        return wrapped

    return decorator


def _record_pipeline_metrics(func: Callable) -> Callable:
    """Create one top-level metrics record for a dynamic set of workflow steps."""

    call_signature = signature(func)

    @wraps(func)
    def wrapped(
        self,
        input_path: str,
        steps,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        normalized_steps = set(steps)
        bound = call_signature.bind(
            self,
            input_path,
            normalized_steps,
            *args,
            **kwargs,
        )
        bound.apply_defaults()
        with self._runtime.run_metrics_session(
            input_path,
            operation="pipeline",
            requested_steps=sorted(normalized_steps),
            invocation={
                "out_format": bound.arguments["out_format"],
                "pdf_engine": bound.arguments["pdf_engine"],
            },
        ):
            return func(
                self,
                input_path,
                normalized_steps,
                *args,
                **kwargs,
            )

    return wrapped


class PipelineRuntime:
    """Shared pipeline clients, agents, accounting, metrics, languages and source identity."""

    def __init__(self, config: Config, client: LLMClient | None = None):
        """Initialize the shared LLM client, usage checkpoint and pipeline agents."""
        self.config = config
        self.client = client or build_client(config)
        # Client usage is cumulative in-process; checkpoints isolate newly accrued usage at each flush.
        self._usage_checkpoint = self.client.usage_summary()
        self.analyzer = Analyzer(self.client, config)
        self.synopsizer = Synopsizer(self.client, config)
        self.translator = Translator(self.client, config)
        self.reviewer = Reviewer(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)
        self.annotation_aligner = AnnotationAligner(self.client, config)
        self._active_run_metrics: RunMetricsRecorder | None = None
        self._run_metrics_suppressed = False

    # Events and usage.
    def log_event(self, store: RunStore, event: str, **payload: Any) -> None:
        """Append a run-level event to the current book's event log."""
        store.log_event(event, **payload)

    def bind_llm_events(self, store: RunStore) -> None:
        """Append provider retry events to the current book log as they occur."""
        self.client.set_event_sink(store.log_event)

    def export_punctuation_enabled(self) -> bool:
        """Determine whether export copies should use Simplified Chinese punctuation
        normalization.
        """
        target = (self.config.target_lang or "").lower().replace("_", "-")
        return self.config.output.punctuation_normalize and require_language(target) == "zh"

    def flush_usage(self, store: RunStore, *, scope: str) -> dict[str, Any]:
        """Merge the client's unpersisted usage delta into the book's usage.json."""
        current = self.client.usage_summary()
        increment = usage_delta(current, self._usage_checkpoint)
        self._usage_checkpoint = current
        accumulated = store.load_usage() or {
            "totals": {},
            "by_tier": {},
            "by_stage": {},
        }
        if not increment["totals"]["calls"]:
            return merge_usage_summaries(accumulated, increment)
        cumulative = merge_usage_summaries(accumulated, increment)
        store.save_usage(cumulative)
        store.log_event(
            "usage_summary",
            scope=scope,
            increment=increment,
            cumulative=cumulative,
        )
        return cumulative

    # Run metrics.
    @contextmanager
    def run_metrics_session(
        self,
        input_path: str,
        *,
        operation: str,
        requested_steps: list[str],
        invocation: dict[str, Any] | None = None,
    ) -> Iterator[RunMetricsRecorder | None]:
        """Create a top-level operation ledger; nested entry points reuse the same record."""
        active = self._active_run_metrics
        if active is not None:
            yield active
            return
        if self._run_metrics_suppressed or not _RUN_METRICS_ENABLED:
            yield None
            return

        try:
            recorder = RunMetricsRecorder.start(
                operation=operation,
                requested_steps=requested_steps,
                input_path=input_path,
                config=self.config,
                client=self.client,
                invocation=invocation,
            )
        except Exception as metrics_error:
            warnings.warn(
                f"Cannot start run metrics: {type(metrics_error).__name__}",
                RuntimeWarning,
                stacklevel=2,
            )
            self._run_metrics_suppressed = True
            try:
                yield None
            finally:
                self._run_metrics_suppressed = False
            return

        self._active_run_metrics = recorder
        status = "failed"
        error: BaseException | None = None
        try:
            yield recorder
            status = "completed"
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                recorder.finish(self.client, status=status, error=error)
            except Exception as metrics_error:
                warnings.warn(
                    f"Cannot save run metrics: {type(metrics_error).__name__}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            self._active_run_metrics = None

    @contextmanager
    def metric_stage(self, name: str) -> Iterator[None]:
        """Measure stage duration when a ledger exists; otherwise preserve ordinary behavior."""
        if self._active_run_metrics is None:
            yield
            return
        with self._active_run_metrics.stage(name):
            yield

    def measure_stage_call(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Measure a function's stage while preserving its result or exception."""
        with self.metric_stage(name):
            return func(*args, **kwargs)

    def attach_metrics_store(self, store: RunStore) -> None:
        """Persist the top-level run ledger alongside current book state."""
        if self._active_run_metrics is not None:
            try:
                self._active_run_metrics.attach_store(store)
            except Exception as metrics_error:
                warnings.warn(
                    f"Cannot bind run metrics: {type(metrics_error).__name__}",
                    RuntimeWarning,
                    stacklevel=2,
                )

    def capture_metrics_state(self, store: RunStore) -> None:
        """Freeze final live/snapshot state so later progress cannot alter the ledger."""
        if self._active_run_metrics is None:
            return
        try:
            self._active_run_metrics.capture_state(store)
        except Exception as metrics_error:
            warnings.warn(
                f"Cannot capture final run state: {type(metrics_error).__name__}",
                RuntimeWarning,
                stacklevel=2,
            )

    # Source identity.
    def source_sha256(self, input_path: str) -> str:
        """Rehash at state-consumption boundaries; never trust metrics snapshots captured
        outside the lock.
        """
        if self._active_run_metrics is not None:
            verified = self._active_run_metrics.verify_input_sha256(input_path)
            if verified is not None:
                return verified
        digest = source_sha256(input_path)
        if self._active_run_metrics is not None:
            self._active_run_metrics.input["sha256"] = digest
        return digest

    def initial_source_sha256(self, input_path: str) -> str:
        """Capture pre-parse identity, reusing the metrics startup snapshot when available to
        avoid rereading.
        """
        if self._active_run_metrics is not None:
            initial = self._active_run_metrics.input.get("sha256")
            if isinstance(initial, str):
                return initial
        return source_sha256(input_path)

    def ensure_store_source(self, store: RunStore, input_path: str) -> str:
        """Validate that candidate state belongs to the current input."""
        validate_run_languages(
            store.load_manifest(), self.config.source_lang, self.config.target_lang
        )
        return store.ensure_source_identity(
            input_path,
            actual_sha256=self.source_sha256(input_path),
        )

    # Language resolution.
    def apply_language(self, lang: str) -> None:
        """Apply detected source language to config and all agents after auto detection."""
        resolved = lang or self.config.source_lang
        source = require_language(resolved)
        target = require_language(self.config.target_lang)
        if source and target and source == target:
            raise ValueError(
                f"Source and target languages are identical ({source}); no translation is needed. "
                "Change language.source or language.target in config.yaml."
            )
        self.config.source_lang = source
        self.config.target_lang = target
        for ag in (
            self.analyzer,
            self.synopsizer,
            self.translator,
            self.reviewer,
            self.polisher,
            self.extractor,
            self.annotation_aligner,
        ):
            ag.src = source
            ag.tgt = self.config.target_lang

    def apply_manifest_languages(self, manifest: dict[str, Any]) -> None:
        """Restore saved source/target languages and propagate them to all agents."""
        validate_run_languages(manifest, self.config.source_lang, self.config.target_lang)
        source = manifest.get("source_lang")
        self.apply_language(
            source if isinstance(source, str) and source else self.config.source_lang
        )
