"""Shared runtime owning Config, LLMClient and every agent for one Orchestrator.
This is neither a global singleton nor a guarantee of concurrent reuse across books.
Centralize construction to avoid duplicate clients/accounting or lost language state.
Restore manifest languages into config and all agents.
Own event sinks, usage checkpoints/flushes, language restoration and source hashes.
"""

from __future__ import annotations

from typing import Any

from ..agents.analyzer import Analyzer
from ..agents.annotation_aligner import AnnotationAligner
from ..agents.polisher import Polisher
from ..agents.reviewer import Reviewer
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..i18n.languages import require_language, validate_run_languages
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from .runstore import RunStore, source_sha256


class PipelineRuntime:
    """Shared pipeline clients, agents, accounting, languages and source identity."""

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

    # Source identity.
    def ensure_store_source(self, store: RunStore, input_path: str) -> str:
        """Validate that candidate state belongs to the current input."""
        validate_run_languages(
            store.load_manifest(), self.config.source_lang, self.config.target_lang
        )
        return store.ensure_source_identity(
            input_path,
            actual_sha256=source_sha256(input_path),
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
        self.apply_language(manifest["source_lang"])
