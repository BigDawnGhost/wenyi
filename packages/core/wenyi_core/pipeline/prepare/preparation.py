"""AI preparation: language detection, chapter prescan and style analysis.
Reuse model-free input preparation for source parsing and state location. Own sample
selection and rolling context. Initialize derived state first, commit the
initialized manifest last, then finish initialization. Build the book synopsis afterward as
configured. Share pure language normalization with Runtime through top-level i18n.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ...agents.analyzer import analysis_digest_budget
from ...assemble.writer_common import validate_pdf_source_format
from ...events import ProgressFn
from ...i18n.languages import normalize_language
from ...i18n.policy.models import Phase, content_hash
from ...i18n.prompts import render
from ...ingest.epub_reader import peek_epub_title
from ...ingest.segmenter import load_document
from ...ingest.tokens import count_tokens
from ...llm.errors import ProviderFailure, describe_provider_failure
from ...llm.routing import inference_snapshot
from ...models.document import Document
from ...storage.protocol import Storage
from ...storage.run_store import source_sha256, translation_run_dir
from ..run.language_policies import commit_revision, initialize_policies, translation_revision
from .book_understanding import BookUnderstandingService
from .epub_objects import refresh_epub_objects
from .input_preparation import (
    locate_input_storage,
    parse_document,
)

if TYPE_CHECKING:
    from ..run.runtime import PipelineRuntime


class LanguageDetectionError(ValueError):
    """Preserve a safe provider diagnostic instead of reporting an invalid language."""

    def __init__(self, failure: ProviderFailure):
        self.failure = failure
        status = f" (HTTP {failure.status_code})" if failure.status_code is not None else ""
        super().__init__(f"Source language detection request failed{status}: {failure.message}")


class PreparationService:
    """Domain service for state lookup, parsing, initialization and book understanding."""

    def __init__(
        self,
        runtime: PipelineRuntime,
        *,
        mineru_token_resolver: Callable[[], str | None] | None = None,
    ):
        self._runtime = runtime
        self._mineru_token_resolver = mineru_token_resolver
        self._understanding = BookUnderstandingService(
            runtime.config, runtime.synopsizer, runtime.analyzer, runtime.usage
        )

    # State lookup and resume.
    def locate_existing(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> Storage:
        """Locate existing state without creating or initializing a translation task.
        PDF state follows the filename and can be checked before MinerU. EPUB needs only the
        OPF title, avoiding full-resource annotation that export will later repeat. Other
        formats parse their local title to match preparation's state path.
        """
        if self._runtime.storage is not None:
            store = self._runtime.storage
            if not store.exists():
                raise ValueError("No translation progress found. Run translate first.")
            self._runtime.ensure_store_source(store, input_path)
            self._runtime.bind_llm_events(store)
            return store
        ext = os.path.splitext(input_path)[1].lower()
        if ext == ".pdf":
            title = os.path.splitext(os.path.basename(input_path))[0]
        elif ext == ".epub":
            if progress:
                progress(0, 0, "progress.stage.locateProgress")
            title = peek_epub_title(input_path)
        else:
            if progress:
                progress(0, 0, "progress.stage.locateProgress")
            doc = load_document(
                input_path,
                self._runtime.config.source_lang,
                self._runtime.config.target_lang,
                split_segments=self._runtime.config.segment.max_tokens_per_segment,
            )
            title = doc.title

        store = self._runtime.get_store(
            translation_run_dir(
                self._runtime.config.state_dir, title, self._runtime.config.target_lang
            ),
            create=False,
        )
        if not store.exists():
            raise ValueError("No translation progress found. Run translate first.")
        self._runtime.ensure_store_source(store, input_path)
        self._runtime.bind_llm_events(store)

        return store

    def prepare(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> Storage:
        """Parse input and locate state; initialize first runs under the book lock.
        PDF state follows the filename, allowing manifest checks before repeated external
        conversion. The derived source EPUB is stored separately from the original PDF.
        """
        self._runtime.require_glossary_note_access()
        if self._runtime.storage is not None:
            store = self._runtime.storage
            source_hash = source_sha256(input_path)
        else:
            store, source_hash = locate_input_storage(
                input_path, self._runtime.config, self._runtime.get_store, progress=progress
            )
        self._runtime.bind_llm_events(store)

        with store.lock():
            if store.exists():
                return self._resume_existing(store, input_path)
            if os.path.splitext(input_path)[1].lower() == ".pdf":
                # Preserve the source identity and event history when conversion fails.
                store.begin_initialization(source_hash)
            doc = parse_document(
                store,
                input_path,
                self._runtime.config,
                expected_sha256=source_hash,
                progress=progress,
                mineru_token_resolver=self._mineru_token_resolver,
            )
            return self._prepare_locked(
                doc,
                store,
                input_path,
                progress,
                source_hash=source_hash,
            )

    def _resume_existing(
        self, store: Storage, input_path: str, *, document: Document | None = None
    ) -> Storage:
        self._runtime.ensure_store_source(store, input_path)
        validate_pdf_source_format(store.load_manifest())
        refresh_epub_objects(store, input_path, document=document)
        store.log_event("run_resumed", input_path=input_path, run_dir=store.run_dir)
        return store

    def _prepare_locked(
        self,
        doc,
        store: Storage,
        input_path: str,
        progress: ProgressFn | None,
        *,
        source_hash: str,
    ) -> Storage:
        """Restore existing state, or write new derived state before atomically committing the
        manifest.
        """
        if store.exists():
            return self._resume_existing(store, input_path, document=doc)

        store.begin_initialization(source_hash)

        try:
            return self._initialize_document(doc, store, input_path, progress, source_hash)
        finally:
            self._runtime.usage.flush(store, scope="prepare")

    def _initialize_document(
        self,
        doc: Document,
        store: Storage,
        input_path: str,
        progress: ProgressFn | None,
        source_hash: str,
    ) -> Storage:
        """Build a new run after source identity is established by initialization."""
        # For new auto-language runs, use model detection only; require an explicit language on failure.
        if self._runtime.config.source_lang in ("auto", "", None):
            if progress:
                progress(0, 0, "progress.stage.language")
            try:
                detected = self.detect_language_ai(doc)
            except LanguageDetectionError as error:
                store.log_event(
                    "language_detection_failed",
                    source_lang=doc.source_lang,
                    operation="language.detect",
                    error_type=type(error.__cause__).__name__,
                    **error.failure.log_fields(),
                )
                raise
            if not detected:
                store.log_event(
                    "language_detection_failed",
                    source_lang=doc.source_lang,
                    reason="unsupported_language_result",
                    error_category="unsupported_language_result",
                    error_message="Language detection returned no supported language code.",
                )
                raise ValueError(
                    "Source language detection returned no supported language. Set "
                    "language.source in config.yaml to a supported code, such as ja/en/zh-Hant/ko/fr/de/es."
                )
            doc.source_lang = detected
            store.log_event("language_detected", source_lang=doc.source_lang)
        self._runtime.apply_language(doc.source_lang, source_identity=source_hash)
        doc.source_lang = self._runtime.config.source_lang
        doc.target_lang = self._runtime.config.target_lang

        manifest = store.stage_document(
            doc,
            source_hash=source_hash,
        )
        manifest["language_policies"] = initialize_policies(store, self._runtime.config)
        digests = None
        if self._runtime.config.pipeline.book_understanding:
            digests = self._understanding.ensure_chapter_digests(store, doc.chapters, progress)
        if progress:
            progress(0, 0, "progress.stage.style")
        analysis = self._analyze_document(store, doc, digests, source_hash)
        analysis["language_policy"] = manifest["language_policies"]["analysis"]
        analysis["style_policy"] = self._runtime.config.language_policy(
            "analysis"
        ).task_fingerprint("analyzer")
        store.save_analysis(analysis)
        store.log_event("analysis_saved", has_analysis=bool(analysis))

        # The manifest marks successful initialization and must be committed atomically last.
        manifest["initialized"] = True
        store.save_manifest(manifest)
        self._runtime.bind_timing(store)
        store.finish_initialization()
        store.log_event(
            "run_initialized",
            input_path=input_path,
            run_dir=store.run_dir,
            title=doc.title,
            fmt=doc.fmt,
            source_lang=doc.source_lang,
            target_lang=doc.target_lang,
            chapters=len(doc.chapters),
            skipped_boilerplate_count=int(doc.meta.get("skipped_boilerplate_count") or 0),
            config={
                "review": self._runtime.config.pipeline.review,
                "polish": self._runtime.config.pipeline.polish,
                "book_understanding": self._runtime.config.pipeline.book_understanding,
                "review_concurrency": self._runtime.config.pipeline.review_concurrency,
                "review_output_retries": (self._runtime.config.pipeline.review_output_retries),
            },
        )
        return store

    def _analyze_document(
        self,
        store: Storage,
        document: Document,
        digests: list[str] | None,
        source_hash: str,
    ) -> dict[str, Any]:
        """Cache paid style analysis independently of the initialization manifest."""
        sample = self.sample_text(document)
        if not sample:
            return {}
        policy = self._runtime.config.language_policy("analysis")
        src = self._runtime.config.source_lang
        tgt = self._runtime.config.target_lang
        digest_budget = analysis_digest_budget(
            self._runtime.config,
            render("analyzer_system", plan=policy, src=src, tgt=tgt),
            render("analyzer_user", plan=policy, src=src, tgt=tgt, sample=sample),
        )
        key_payload: dict[str, Any] = {
            "source": source_hash,
            "sample": sample,
            "digests": digests,
            "policy": policy.task_fingerprint("analyzer"),
            "routing": inference_snapshot(self._runtime.config.llm, ("analysis.style",)),
        }
        # Only bind digest_budget when the raw digest block exceeds it, preserving existing
        # cache keys for small books whose digests render byte-for-byte identically.
        if (
            digests
            and count_tokens("\n".join(f"[{i}] {digest}" for i, digest in enumerate(digests)))
            > digest_budget
        ):
            key_payload["digest_budget"] = digest_budget
        key = "preparation/analysis/" + content_hash(key_payload) + ".json"
        saved = store.read_artifact(key)
        if isinstance(saved, dict):
            return saved
        try:
            analysis = (
                self._runtime.analyzer.analyze(sample, chapter_digests=digests)
                if digests is not None
                else self._runtime.analyzer.analyze(sample)
            )
            store.write_artifact(key, analysis)
            return analysis
        finally:
            self._runtime.usage.flush(store, scope="analysis")

    def activate(self, store: Storage, *, phase: Phase | None = None) -> dict[str, Any]:
        """Restore manifest languages, propagate them to all agents and return the manifest."""
        store.recover_usage()
        manifest = store.load_manifest()
        self._runtime.apply_manifest_languages(manifest)
        if phase == "translation":
            self._runtime.require_glossary_note_access()
        if phase == "translation" and translation_revision(store, self._runtime.config):
            self._rebuild_analysis(store, manifest)
        return manifest

    def _rebuild_analysis(self, store: Storage, manifest: dict[str, Any]) -> None:
        """Refresh built-in guidance while preserving formal targets and glossary."""
        chapters = [store.load_chapter(row["index"]) for row in manifest["chapters"]]
        digests = None
        if self._runtime.config.pipeline.book_understanding:
            digests = self._understanding.ensure_chapter_digests(store, chapters, None)
        # Assemble only the source samples; formal chapters are never rewritten here.
        document = Document(
            title=manifest.get("title", ""),
            source_lang=self._runtime.config.source_lang,
            target_lang=self._runtime.config.target_lang,
            fmt=manifest.get("fmt", "text"),
            chapters=chapters,
        )
        style_policy = self._runtime.config.language_policy("analysis").task_fingerprint("analyzer")
        analysis = store.load_analysis() or {}
        if analysis.get("style_policy") != style_policy:
            analysis = self._analyze_document(
                store, document, digests, manifest.get("source_sha256", "")
            )
        analysis["style_policy"] = style_policy
        analysis["language_policy"] = (
            f"language-policies/{self._runtime.config.language_policy('analysis').fingerprint}.json"
        )
        store.save_analysis(analysis)
        commit_revision(store, self._runtime.config)
        store.log_event(
            "language_policy_revision_refreshed", rebuild="analysis", completed_targets="preserved"
        )

    def detect_language_ai(self, doc) -> str:
        """Return a supported language code, or surface model request failures distinctly."""
        # Use unlabeled source samples so sampling labels cannot contaminate language detection.
        sample = self.sample_text(doc, labeled=False)[:1500]
        if not sample.strip():
            return ""
        system = render("language_detector_system")
        try:
            data = self._runtime.client.complete_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": sample},
                ],
                operation="language.detect",
            )
            code = (data.get("language") if isinstance(data, dict) else "") or ""
            return normalize_language(str(code))
        except Exception as error:
            raise LanguageDetectionError(describe_provider_failure(error)) from error

    @staticmethod
    def sample_text(doc, *, labeled: bool = True) -> str:
        """Select style samples from the beginning, middle and end with labels when requested.
        For language detection, return one pure source sample without labels so the label
        language cannot bias detection.
        """
        texts = ["\n".join(s.source for s in ch.text_segments) for ch in doc.chapters]
        texts = [t for t in texts if len(t) > 200]
        if not texts:  # Fallback when every chapter is short.
            joined = "\n".join(s.source for ch in doc.chapters[:2] for s in ch.text_segments)
            return joined[:6000]
        if not labeled:
            return texts[0][:6000]
        picks = [
            (0, "Opening sample"),
            (len(texts) // 2, "Middle sample"),
            (len(texts) - 1, "Ending sample"),
        ]
        parts: list[str] = []
        seen: set[int] = set()
        for idx, tag in picks:
            if idx in seen:  # Deduplicate samples for short books with one or two chapters.
                continue
            seen.add(idx)
            t = texts[idx]
            chunk = t[-2800:] if tag == "Ending sample" else t[:2800]
            parts.append(f"【{tag}】\n{chunk}")
        return "\n\n".join(parts)

    # Book-understanding prescan: chapter digests and whole-book synopsis.
    def ensure_understanding(
        self,
        store: Storage,
        progress: ProgressFn | None = None,
    ) -> str:
        """Coordinate understanding after initialization; the service owns its checkpoints."""
        return self._understanding.ensure_understanding(store, progress)
