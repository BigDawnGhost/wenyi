"""Preparation: state lookup, parsing, language detection, initialization, analysis and
prescan.
Own PDF conversion caches, source hashes, sample selection, initial glossary and rolling
context. Initialize derived chapters/analysis/glossary/context first, atomically commit the
initialized manifest last, then finish initialization. Build chapter digests and the book
synopsis as configured. Share pure language normalization with Runtime through top-level i18n.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from ..glossary.store import GlossaryStore
from ..i18n.languages import normalize_language
from ..i18n.prompts import render
from ..i18n.resources import prompt_fingerprint
from ..ingest.epub_reader import peek_epub_title
from ..ingest.segmenter import load_document
from .context import RollingContext
from .runstore import RunStore, source_sha256, translation_run_dir

if TYPE_CHECKING:
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


class PreparationService:
    """Domain service for state lookup, parsing, initialization and book understanding."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    # State lookup and resume.
    def locate_existing(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """Locate existing state without creating or initializing a translation task.
        PDF state follows the filename and can be checked before MinerU. EPUB needs only the
        OPF title, avoiding full-resource annotation that export will later repeat. Other
        formats parse their local title to match preparation's state path.
        """
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
                split_segments=self._runtime.config.segment.max_chars_per_segment,
            )
            title = doc.title

        store = RunStore(
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
    ) -> RunStore:
        """Parse input and locate state; initialize first runs under the book lock.
        PDF state follows the filename, allowing manifest checks before repeated external
        conversion. Cache the initial converted HTML within that state directory.
        """
        if os.path.splitext(input_path)[1].lower() == ".pdf":
            # PDF titles use the filename, so the state directory is known before initial parsing.
            pdf_title = os.path.splitext(os.path.basename(input_path))[0]
            run_dir = translation_run_dir(
                self._runtime.config.state_dir, pdf_title, self._runtime.config.target_lang
            )
            store = RunStore(run_dir)
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
                    split_segments=self._runtime.config.segment.max_chars_per_segment,
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
            split_segments=self._runtime.config.segment.max_chars_per_segment,
        )
        if source_sha256(input_path) != source_hash:
            raise ValueError("Source changed during parsing; ensure the file is stable and retry.")
        run_dir = translation_run_dir(
            self._runtime.config.state_dir, doc.title, self._runtime.config.target_lang
        )
        store = RunStore(run_dir)
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
        store: RunStore,
        input_path: str,
        progress: ProgressFn | None,
        *,
        source_hash: str,
    ) -> RunStore:
        """Restore existing state, or write new derived state before atomically committing the
        manifest.
        """
        if store.exists():
            self._runtime.ensure_store_source(store, input_path)
            store.log_event("run_resumed", input_path=input_path, run_dir=store.run_dir)
            return store  # Resume existing progress without reset; run() restores languages from the manifest.

        store.begin_initialization(source_hash)

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
        self._runtime.apply_language(doc.source_lang)
        doc.source_lang = self._runtime.config.source_lang
        doc.target_lang = self._runtime.config.target_lang

        manifest = store.stage_document(
            doc,
            source_hash=source_hash,
        )
        glossary = GlossaryStore(store.glossary_path)
        try:
            if progress:
                progress(0, 0, "Analyzing book style…")
            sample = self.sample_text(doc)
            analysis = self._runtime.analyzer.analyze(sample) if sample else {}
            if analysis:
                self._runtime.analyzer.seed_glossary(glossary, analysis)
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
            manifest["prompt_fingerprint"] = prompt_fingerprint()
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
        finally:
            glossary.close()
        return store

    def activate(self, store: RunStore) -> dict[str, Any]:
        """Restore manifest languages, propagate them to all agents and return the manifest."""
        store.recover_usage()
        manifest = store.load_manifest()
        self._runtime.apply_manifest_languages(manifest)
        store.log_event("language_resources_applied", prompt_fingerprint=prompt_fingerprint())
        return manifest

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
        store: RunStore,
        progress: ProgressFn | None = None,
    ) -> str:
        """Prescan source chapters into chapter.meta digests and an analysis synopsis.
        Skip existing results for idempotent resume. Return the synopsis for translation
        prompts, or empty when book_understanding is disabled.
        """
        if not self._runtime.config.pipeline.book_understanding:
            store.log_event("book_understanding_skipped", reason="disabled")
            return ""
        manifest = store.load_manifest()
        chapters = manifest.get("chapters", [])

        # Digest chapters independently in a thread pool, but persist all results on the main thread
        # to avoid competing atomic writes and preserve incremental chapter-level resume. Skip saved digests.
        loaded = {
            c.get("index", i): store.load_chapter(c.get("index", i)) for i, c in enumerate(chapters)
        }
        todo = [
            (ci, "\n".join(s.source for s in ch.text_segments))
            for ci, ch in loaded.items()
            if not ch.meta.get("source_digest")
        ]
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
                    loaded[ci].meta["source_digest"] = (
                        fut.result()
                    )  # _ask_text already returns an empty fallback on failure.
                    store.save_chapter(loaded[ci])
                    store.log_event(
                        "book_understanding_chapter_digest_saved",
                        chapter=ci,
                        digest=loaded[ci].meta["source_digest"],
                    )
                    if progress:
                        progress(n_done, len(todo), "Prescanning chapter digests")

        # Assemble in manifest chapter order, independent of worker completion order.
        digests = [
            loaded[c.get("index", i)].meta.get("source_digest", "") or ""
            for i, c in enumerate(chapters)
        ]

        analysis = store.load_analysis() or {}
        synopsis = analysis.get("book_synopsis", "")
        if not synopsis and any(d.strip() for d in digests):
            if progress:
                progress(0, 0, "Generating whole-book synopsis…")
            synopsis = self._runtime.synopsizer.book_synopsis(
                digests,
                self._runtime.analyzer.style_brief(analysis),
            )
            analysis["book_synopsis"] = synopsis
            store.save_analysis(analysis)
            store.log_event("book_synopsis_saved", synopsis=synopsis)
        return synopsis
