"""Shared runtime owning Config, LLMClient and every agent for one Orchestrator.
This is neither a global singleton nor a guarantee of concurrent reuse across books.
Centralize construction to avoid duplicate clients/accounting or lost language state.
Restore manifest languages into config and all agents.
Own event sinks, usage checkpoints/flushes, language restoration and source hashes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..agents.analyzer import Analyzer
from ..agents.annotation_aligner import AnnotationAligner
from ..agents.polisher import Polisher
from ..agents.reviewer import Reviewer
from ..agents.synopsis import Synopsizer
from ..agents.terminology import TerminologyAgent
from ..agents.title_translator import TitleTranslator
from ..agents.translator import Translator
from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..i18n.languages import require_language, validate_run_languages
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.routing import resolve_routes
from ..llm.usage import empty_usage, merge_usage_summaries, usage_delta, validate_usage
from ..storage.protocol import Storage
from ..timing import RunTimer
from .runstore import source_sha256


class PipelineRuntime:
    """Shared pipeline clients, agents, accounting, languages and source identity."""

    def __init__(
        self,
        config: Config,
        client: LLMClient | None = None,
        storage: Storage | None = None,
        *,
        allow_terminology_context: bool = False,
    ):
        """Initialize the shared LLM client, usage checkpoint and pipeline agents."""
        self.storage = storage
        self.config = config
        self._allow_terminology_context = allow_terminology_context
        from ..assemble.export_view import TEXT_HANDLERS
        from ..assemble.policy import WRITER_OPERATIONS
        from ..i18n.policy.registry import validate_implementations
        from ..i18n.prompts import PROMPT_OPERATIONS
        from ..llm.operations import OPERATIONS
        from .terminology import EVIDENCE_OPERATIONS

        validate_implementations(
            {
                **WRITER_OPERATIONS,
                "export.text": tuple(TEXT_HANDLERS),
                "prompt.compose": PROMPT_OPERATIONS,
                "evidence.prepare": EVIDENCE_OPERATIONS,
            },
            tuple(OPERATIONS),
        )
        self.llm_config = config.llm.model_copy(deep=True)
        self.client = client or build_client(config)
        self._timer: RunTimer | None = None
        # Client usage is cumulative in-process; checkpoints isolate newly accrued usage at each flush.
        self._usage_checkpoint = self.client.usage_summary()
        self.analyzer = Analyzer(self.client, config)
        self.synopsizer = Synopsizer(self.client, config)
        self.translator = Translator(self.client, config)
        self.title_translator = TitleTranslator(self.client, config)
        self.reviewer = Reviewer(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)
        self.annotation_aligner = AnnotationAligner(self.client, config)
        self.terminology_agent = TerminologyAgent(self.client, config)

    def require_terminology_context_access(self) -> None:
        """Check generation permission, independently of consuming saved glossary notes."""
        if self.config.pipeline.terminology_context and not self._allow_terminology_context:
            raise ValueError(
                "Whole-book terminology evidence is supported only by CLI prepare/translate. "
                "Disable pipeline.terminology_context for this entry point."
            )

    def get_store(self, run_dir: str, *, create: bool = True) -> Storage:
        """Bind injected Web storage or construct CLI file storage at the boundary."""
        if self.storage is not None:
            return self.storage
        from ..storage.file import FileStorage

        return FileStorage(run_dir, create=create)

    @contextmanager
    def track_workflow(self, operation: str) -> Iterator[None]:
        """Count nested stage entry points once, as part of their outer workflow."""
        if self._timer is not None:
            yield
            return
        with RunTimer(operation) as timer:
            self._timer = timer
            try:
                yield
            finally:
                self._timer = None

    def bind_timing(self, store: Storage) -> None:
        """Attach timing after source validation or a successful manifest commit."""
        if self._timer is not None:
            self._timer.store = store

    # Events and usage.
    def log_event(self, store: Storage, event: str, **payload: Any) -> None:
        """Append a run-level event to the current book's event log."""
        store.log_event(event, **payload)

    def bind_llm_events(self, store: Storage) -> None:
        """Append provider retry events to the current book log as they occur."""
        validate_usage(store.load_usage())
        self.client.set_event_sink(store.log_event)
        store.log_event(
            "llm_routing_plan",
            routes={
                operation: route.describe()
                for operation, route in resolve_routes(self.llm_config).items()
            },
        )

    def flush_usage(self, store: Storage, *, scope: str, review=None) -> dict[str, Any]:
        """Merge the client's unpersisted usage delta into the book's usage.json."""
        store.recover_usage()
        current = self.client.usage_summary()
        increment = usage_delta(current, self._usage_checkpoint)
        accumulated = store.load_usage() or empty_usage()
        if not increment["totals"]["calls"]:
            if review is not None and review.load_usage() is None:
                review.save_usage(empty_usage())
            return merge_usage_summaries(accumulated, increment)
        cumulative = merge_usage_summaries(accumulated, increment)
        ledgers = {"usage.json": cumulative}
        if review is not None:
            ledgers[f"reviews/{review.review_id}/usage.json"] = merge_usage_summaries(
                review.load_usage() or empty_usage(), increment
            )
        store.prepare_usage_commit(ledgers)
        self._usage_checkpoint = current
        store.recover_usage()
        store.log_event(
            "usage_summary",
            scope=scope,
            increment=increment,
            cumulative=cumulative,
        )
        return cumulative

    # Source identity.
    def ensure_store_source(self, store: Storage, input_path: str) -> str:
        """Validate that candidate state belongs to the current input."""
        validate_run_languages(
            store.load_manifest(), self.config.source_lang, self.config.target_lang
        )
        digest = store.ensure_source_identity(
            input_path,
            actual_sha256=source_sha256(input_path),
        )
        self.bind_timing(store)
        return digest

    # Language resolution.
    def apply_language(self, lang: str, *, source_identity: str = "") -> None:
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
        self.config.freeze_language_policies(source_identity)
        for ag in (
            self.analyzer,
            self.synopsizer,
            self.translator,
            self.title_translator,
            self.reviewer,
            self.polisher,
            self.extractor,
            self.annotation_aligner,
            self.terminology_agent,
        ):
            ag.src = source
            ag.tgt = self.config.target_lang
            ag.language_policy = self.config.language_policy(ag.policy_phase)

    def apply_manifest_languages(self, manifest: dict[str, Any]) -> None:
        """Restore saved source/target languages and propagate them to all agents."""
        validate_run_languages(manifest, self.config.source_lang, self.config.target_lang)
        self.apply_language(
            manifest["source_lang"], source_identity=manifest.get("source_sha256", "")
        )
