"""Preparation: state lookup, parsing, language detection, chapter prescan and style analysis.
Own PDF conversion caches, source hashes, sample selection, initial glossary and rolling
context. Initialize derived chapters/analysis/glossary/context first, atomically commit the
initialized manifest last, then finish initialization. Build the book synopsis afterward as
configured. Share pure language normalization with Runtime through top-level i18n.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from ..events import ProgressFn
from ..i18n.languages import normalize_language
from ..i18n.policy.models import Phase, content_hash
from ..i18n.prompts import render
from ..ingest.epub_reader import peek_epub_title
from ..ingest.models import Chapter, Document
from ..ingest.segmenter import load_document
from ..storage.protocol import Storage
from .context import RollingContext
from .language_policies import commit_revision, initialize_policies, translation_revision
from .runstore import source_sha256, translation_run_dir
from .terminology import TerminologyService

if TYPE_CHECKING:
    from .runtime import PipelineRuntime

_LOGGER = logging.getLogger(__name__)
_DIGEST_VERSION = 2


def _synopsis_complete(text: str) -> bool:
    """Identify likely finished legacy digests without a completion marker."""
    cleaned = (text or "").strip().rstrip("”\"'」』）)]}")
    return bool(cleaned) and cleaned[-1] in "。．.！？!?…"


def _digest_complete(chapter: Chapter, *, full_coverage: bool = False) -> bool:
    """Use legacy completion checks unless full-source coverage is selected."""
    digest = chapter.meta.get("source_digest")
    return bool(
        isinstance(digest, str)
        and digest.strip()
        and (
            chapter.meta.get("source_digest_complete") is True
            and chapter.meta.get("source_digest_version") == _DIGEST_VERSION
            if full_coverage
            else chapter.meta.get("source_digest_complete") is True or _synopsis_complete(digest)
        )
    )


class PreparationService:
    """Domain service for state lookup, parsing, initialization and book understanding."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    @staticmethod
    def ingest_config(config) -> dict[str, Any]:
        """Fingerprint only parsing inputs shared by upload preview and initialization."""
        return {
            "source_lang": config.source_lang,
            "target_lang": config.target_lang,
            "max_tokens_per_segment": config.segment.max_tokens_per_segment,
            "pdf_backend": config.pipeline.pdf_backend,
            "babeldoc_bridge_url": config.pipeline.babeldoc_bridge_url,
            "babeldoc_pages": config.pipeline.babeldoc_pages,
            "babeldoc_timeout": config.pipeline.babeldoc_timeout,
        }

    @staticmethod
    def load_parsed_document(
        storage, input_path: str, config, *, actual_sha256: str | None = None
    ) -> Document | None:
        """Reuse validated preview parsing; changed input/config triggers normal parsing."""
        cached = storage.read_artifact("parsed_document.json")
        if not isinstance(cached, dict):
            return None
        digest = actual_sha256 or source_sha256(input_path)
        if cached.get("source_sha256") != digest or cached.get(
            "ingest_config"
        ) != PreparationService.ingest_config(config):
            return None
        document = Document.model_validate(cached["document"])
        document.source_path = input_path
        return document

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
                progress(0, 0, "Locating translation progress…")
            title = peek_epub_title(input_path)
        else:
            if progress:
                progress(0, 0, "Locating translation progress…")
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
        conversion. Cache the initial converted HTML within that state directory.
        """
        self._runtime.require_terminology_context_access()
        if self._runtime.storage is not None:
            store = self._runtime.storage
            self._runtime.bind_llm_events(store)
            if store.exists():
                self._runtime.ensure_store_source(store, input_path)
                return store
            digest = source_sha256(input_path)
            cached = self.load_parsed_document(
                store, input_path, self._runtime.config, actual_sha256=digest
            )
            if cached is not None:
                with store.lock():
                    return self._prepare_locked(
                        cached, store, input_path, progress, source_hash=digest
                    )
        if os.path.splitext(input_path)[1].lower() == ".pdf":
            # PDF titles use the filename, so the state directory is known before initial parsing.
            pdf_title = os.path.splitext(os.path.basename(input_path))[0]
            run_dir = translation_run_dir(
                self._runtime.config.state_dir, pdf_title, self._runtime.config.target_lang
            )
            store = self._runtime.get_store(run_dir)
            self._runtime.bind_llm_events(store)

            with store.lock():
                if store.exists():
                    self._runtime.ensure_store_source(store, input_path)
                    store.log_event(
                        "run_resumed",
                        input_path=input_path,
                        run_dir=store.run_dir,
                    )
                    return store
                if progress:
                    progress(0, 0, "Parsing document…")
                source_hash = source_sha256(input_path)
                # Preserve the source identity and event history when conversion fails.
                store.begin_initialization(source_hash)
                pipeline = self._runtime.config.pipeline
                doc = load_document(
                    input_path,
                    self._runtime.config.source_lang,
                    self._runtime.config.target_lang,
                    split_segments=self._runtime.config.segment.max_tokens_per_segment,
                    cache_dir=store.source_dir,
                    source_hash=source_hash,
                    pdf_backend=pipeline.pdf_backend,
                    babeldoc_bridge_url=pipeline.babeldoc_bridge_url,
                    babeldoc_pages=pipeline.babeldoc_pages,
                    babeldoc_timeout=pipeline.babeldoc_timeout,
                )
                if source_sha256(input_path) != source_hash:
                    raise ValueError(
                        "PDF changed during parsing; ensure the file is stable and retry."
                    )
                return self._prepare_locked(
                    doc,
                    store,
                    input_path,
                    progress,
                    source_hash=source_hash,
                )

        if progress:
            progress(0, 0, "Parsing document…")
        source_hash = source_sha256(input_path)
        # Split long paragraphs at sentences and mark continuations for later backfill merging.
        doc = load_document(
            input_path,
            self._runtime.config.source_lang,
            self._runtime.config.target_lang,
            split_segments=self._runtime.config.segment.max_tokens_per_segment,
        )
        if source_sha256(input_path) != source_hash:
            raise ValueError("Source changed during parsing; ensure the file is stable and retry.")
        run_dir = translation_run_dir(
            self._runtime.config.state_dir, doc.title, self._runtime.config.target_lang
        )
        store = self._runtime.get_store(run_dir)
        self._runtime.bind_llm_events(store)

        with store.lock():
            return self._prepare_locked(
                doc,
                store,
                input_path,
                progress,
                source_hash=source_hash,
            )

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
            self._runtime.ensure_store_source(store, input_path)
            store.log_event("run_resumed", input_path=input_path, run_dir=store.run_dir)
            return store  # Resume existing progress without reset; run() restores languages from the manifest.

        store.begin_initialization(source_hash)

        try:
            return self._initialize_document(doc, store, input_path, progress, source_hash)
        finally:
            self._runtime.flush_usage(store, scope="prepare")

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
                progress(0, 0, "Detecting language…")
            detected = self.detect_language_ai(doc)
            if not detected:
                store.log_event("language_detection_failed", source_lang=doc.source_lang)
                raise ValueError(
                    "Source language detection failed. Check model settings or set "
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
            digests = self._ensure_chapter_digests(store, doc.chapters, progress)
        if progress:
            progress(0, 0, "Analyzing book style…")
        analysis = self._analyze_document(store, doc, digests, source_hash)
        analysis["language_policy"] = manifest["language_policies"]["analysis"]
        analysis["style_policy"] = self._runtime.config.language_policy(
            "analysis"
        ).task_fingerprint("analyzer")
        if analysis:
            self._seed_analysis(store, doc.chapters, analysis, progress)
        store.save_analysis(analysis)
        store.log_event("analysis_saved", has_analysis=bool(analysis))
        store.save_context(
            RollingContext(
                max_recent_keep=max(
                    40,
                    self._runtime.config.pipeline.rolling_context_segments,
                )
            ).to_dict()
        )

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
        """Cache paid analysis before fallible admission, independently of the manifest."""
        sample = self.sample_text(document)
        if not sample:
            return {}
        policy = self._runtime.config.language_policy("analysis")
        cached = policy.enabled("terminology.context")
        if not cached:
            return self._runtime.analyzer.analyze(sample)
        key = (
            "preparation/analysis/"
            + content_hash(
                {
                    "source": source_hash,
                    "sample": sample,
                    "digests": digests,
                    "policy": policy.task_fingerprint("analyzer"),
                    "routing": self._runtime.config.llm.model_dump(mode="json"),
                }
            )
            + ".json"
        )
        saved = store.read_artifact(key) if cached else None
        if isinstance(saved, dict):
            return saved
        try:
            analysis = (
                self._runtime.analyzer.analyze(sample, chapter_digests=digests)
                if digests is not None
                else self._runtime.analyzer.analyze(sample)
            )
            if cached:
                store.write_artifact(key, analysis)
            return analysis
        finally:
            if cached:
                self._runtime.flush_usage(store, scope="analysis")

    def _seed_analysis(
        self,
        store: Storage,
        chapters: list[Chapter],
        analysis: dict[str, Any],
        progress: ProgressFn | None = None,
    ) -> None:
        policy = self._runtime.config.language_policy("analysis")
        if not policy.enabled("terminology.context"):
            self._runtime.analyzer.seed_glossary(store, analysis)
            return
        self._runtime.require_terminology_context_access()
        if progress:
            progress(0, 0, "Collecting whole-book terminology evidence…")
        terminology = TerminologyService(
            store,
            self._runtime.terminology_agent,
            chapters,
            checkpoint_usage=lambda: self._runtime.flush_usage(store, scope="terminology"),
        )
        self._runtime.analyzer.seed_glossary(store, analysis, admit=terminology.admit)

    def activate(self, store: Storage, *, phase: Phase | None = None) -> dict[str, Any]:
        """Restore manifest languages, propagate them to all agents and return the manifest."""
        store.recover_usage()
        manifest = store.load_manifest()
        self._runtime.apply_manifest_languages(manifest)
        if phase == "translation":
            self._runtime.require_terminology_context_access()
        if phase == "translation" and translation_revision(store, self._runtime.config):
            self._rebuild_analysis(store, manifest)
        return manifest

    def _rebuild_analysis(self, store: Storage, manifest: dict[str, Any]) -> None:
        """Refresh built-in guidance while preserving formal targets and glossary."""
        chapters = [store.load_chapter(row["index"]) for row in manifest["chapters"]]
        digests = None
        if self._runtime.config.pipeline.book_understanding:
            digests = self._ensure_chapter_digests(store, chapters, None)
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
        if self._runtime.config.language_policy("analysis").enabled("terminology.context"):
            self._seed_analysis(store, chapters, analysis)
        store.save_analysis(analysis)
        commit_revision(store, self._runtime.config)
        store.log_event(
            "language_policy_revision_refreshed", rebuild="analysis", completed_targets="preserved"
        )

    def detect_language_ai(self, doc) -> str:
        """Detect the primary source language with the model; return its code or empty on
        failure.
        """
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
        except Exception:  # noqa: BLE001 - provider errors mean detection failed
            return ""

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
        """Prescan source chapters into chapter.meta digests and an analysis synopsis.
        Require digests for nonempty chapters and reuse verified synopsis caches on resume.
        Return empty when book understanding is disabled or optional synopsis synthesis fails.
        """
        if not self._runtime.config.pipeline.book_understanding:
            store.log_event("book_understanding_skipped", reason="disabled")
            return ""
        manifest = store.load_manifest()
        chapters = manifest.get("chapters", [])

        loaded = [store.load_chapter(row.get("index", i)) for i, row in enumerate(chapters)]
        digests = self._ensure_chapter_digests(store, loaded, progress)
        if not digests:
            return ""

        analysis = store.load_analysis() or {}
        enabled = self._runtime.config.language_policy("analysis").enabled("terminology.context")
        terms = sorted(store.all_terms(), key=lambda term: term.source) if enabled else []
        style = (
            self._runtime.analyzer.style_brief(analysis, terms=terms)
            if enabled
            else self._runtime.analyzer.style_brief(analysis)
        )
        inputs = {
            "language_policy": self._runtime.config.language_policy("analysis").task_fingerprint(
                "book_synopsis"
            ),
            "chapters": [
                [chapter.index, chapter.meta["source_digest"]]
                for chapter in loaded
                if chapter.text_segments
            ],
            "style": style,
            "source_lang": self._runtime.config.source_lang,
            "target_lang": self._runtime.config.target_lang,
        }
        if enabled:
            inputs["glossary"] = [
                [term.source, term.target, term.type, term.gender, term.note] for term in terms
            ]
            inputs["routing"] = self._runtime.config.llm.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        metadata = {"version": 2 if enabled else 1, "inputs_sha256": fingerprint}
        synopsis = analysis.get("book_synopsis", "")
        if (
            isinstance(synopsis, str)
            and synopsis.strip()
            and analysis.get("book_synopsis_meta") == metadata
        ):
            return synopsis
        if progress:
            progress(0, 0, "Generating whole-book synopsis…")
        synopsis = (
            self._runtime.synopsizer.book_synopsis(digests, style, glossary=terms)
            if enabled
            else self._runtime.synopsizer.book_synopsis(digests, style)
        )
        if synopsis:
            analysis["book_synopsis"] = synopsis
            analysis["book_synopsis_meta"] = metadata
            store.save_analysis(analysis)
            store.log_event("book_synopsis_saved", synopsis=synopsis)
        else:
            store.log_event("book_synopsis_failed", reason="generation_failed")
            _LOGGER.warning(
                "Whole-book synopsis generation failed; translation continues with chapter digests. "
                "The synopsis will be retried on the next prepare/translate run."
            )
        return synopsis

    def _ensure_chapter_digests(
        self,
        store: Storage,
        chapters: list[Chapter],
        progress: ProgressFn | None,
    ) -> list[str]:
        """Checkpoint all incurred requests even when prescan cannot finish."""
        try:
            return self._build_chapter_digests(store, chapters, progress)
        finally:
            self._runtime.flush_usage(store, scope="prescan")

    def _build_chapter_digests(
        self,
        store: Storage,
        chapters: list[Chapter],
        progress: ProgressFn | None,
    ) -> list[str]:
        """Prescan staged or initialized chapters and return digests in source order."""
        # Digest chapters independently in a thread pool, but persist all results on the main thread
        # to avoid competing atomic writes and preserve incremental chapter-level resume. Skip saved digests.
        loaded = {chapter.index: chapter for chapter in chapters}
        sources = {
            ci: "\n".join(s.source for s in ch.text_segments)
            for ci, ch in loaded.items()
            if ch.text_segments
        }
        policy = self._runtime.config.language_policy("analysis")
        enabled = policy.enabled("terminology.context")
        fingerprint = content_hash(
            [
                self._runtime.config.language_policy("analysis").task_fingerprint("chapter_digest"),
                self._runtime.config.llm.model_dump(mode="json"),
            ]
        )
        if not enabled:
            fingerprint = policy.task_fingerprint("chapter_digest")
        source_hashes = {
            ci: hashlib.sha256(source.encode("utf-8")).hexdigest() for ci, source in sources.items()
        }
        cache_keys = {}
        if self._runtime.config.language_policy("analysis").enabled("terminology.context"):
            for ci in sources:
                cache_keys[ci] = (
                    "preparation/digests/"
                    + content_hash(
                        [
                            _DIGEST_VERSION,
                            source_hashes[ci],
                            fingerprint,
                            self._runtime.config.llm.model_dump(mode="json"),
                        ]
                    )
                    + ".json"
                )
                saved = store.read_artifact(cache_keys[ci])
                if (
                    (
                        not _digest_complete(loaded[ci], full_coverage=True)
                        or loaded[ci].meta.get("source_digest_policy") != fingerprint
                        or loaded[ci].meta.get("source_digest_sha256") != source_hashes[ci]
                    )
                    and isinstance(saved, dict)
                    and isinstance(saved.get("digest"), str)
                    and saved["digest"]
                ):
                    loaded[ci].meta.update(
                        source_digest=saved["digest"],
                        source_digest_complete=True,
                        source_digest_policy=fingerprint,
                        source_digest_version=_DIGEST_VERSION,
                        source_digest_sha256=source_hashes[ci],
                    )
                    store.save_chapter(loaded[ci])
        todo = [
            (ci, source)
            for ci, source in sources.items()
            if not _digest_complete(loaded[ci], full_coverage=enabled)
            or loaded[ci].meta.get("source_digest_policy") != fingerprint
            or enabled
            and loaded[ci].meta.get("source_digest_sha256") != source_hashes[ci]
        ]
        failed: list[int] = []
        error: Exception | None = None
        if todo:
            store.log_event(
                "book_understanding_chapter_digest_started",
                chapters=[ci for ci, _ in todo],
                workers=max(1, self._runtime.config.pipeline.prescan_concurrency),
            )
            workers = max(1, self._runtime.config.pipeline.prescan_concurrency)
            if progress:
                progress(0, len(todo), "Prescanning chapter digests")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {
                    ex.submit(self._runtime.synopsizer.digest_chapter, src): ci for ci, src in todo
                }
                for n_done, fut in enumerate(as_completed(futs), 1):
                    ci = futs[fut]
                    try:
                        digest = fut.result().strip()
                    except Exception as exc:
                        # Drain submitted work and save other successful chapters before
                        # propagating the original provider/cancellation failure.
                        error = error or exc
                        digest = ""
                    if digest:
                        loaded[ci].meta["source_digest"] = digest
                        loaded[ci].meta["source_digest_complete"] = True
                        loaded[ci].meta["source_digest_policy"] = fingerprint
                        if enabled:
                            loaded[ci].meta["source_digest_version"] = _DIGEST_VERSION
                            loaded[ci].meta["source_digest_sha256"] = source_hashes[ci]
                        if ci in cache_keys:
                            store.write_artifact(cache_keys[ci], {"digest": digest})
                        store.save_chapter(loaded[ci])
                        store.log_event(
                            "book_understanding_chapter_digest_saved", chapter=ci, digest=digest
                        )
                    else:
                        failed.append(ci)
                        store.log_event("book_understanding_chapter_digest_failed", chapter=ci)
                    if progress:
                        progress(n_done, len(todo), "Prescanning chapter digests")

        if error is not None:
            raise error
        if failed:
            indices = ", ".join(str(ci) for ci in sorted(failed))
            raise ValueError(
                f"Chapter digests could not be generated for chapters: {indices}. "
                "Retry prepare/translate; completed digests have been saved."
            )

        # Assemble in manifest chapter order, independent of worker completion order.
        return [loaded[ci].meta["source_digest"] for ci in sources]
