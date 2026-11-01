"""Resumable chapter digests and whole-book synthesis, independent of runtime assembly."""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..agents.analyzer import Analyzer
from ..agents.synopsis import Synopsizer
from ..config import Config
from ..events import ProgressFn
from ..i18n.policy.models import content_hash
from ..ingest.models import Chapter
from ..storage.protocol import Storage
from .runtime_usage import UsageCoordinator

_LOGGER = logging.getLogger(__name__)
_DIGEST_VERSION = 2


def _digest_complete(chapter: Chapter) -> bool:
    """Only verified full-source digests satisfy preparation."""
    digest = chapter.meta.get("source_digest")
    return bool(
        isinstance(digest, str)
        and digest.strip()
        and chapter.meta.get("source_digest_complete") is True
        and chapter.meta.get("source_digest_version") == _DIGEST_VERSION
    )


class BookUnderstandingService:
    """Own prescan concurrency and cache identity, not initialization or model construction."""

    def __init__(
        self, config: Config, synopsizer: Synopsizer, analyzer: Analyzer, usage: UsageCoordinator
    ):
        self._config = config
        self._synopsizer = synopsizer
        self._analyzer = analyzer
        self._usage = usage

    def ensure_understanding(
        self,
        store: Storage,
        progress: ProgressFn | None = None,
    ) -> str:
        """Prescan source chapters into chapter.meta digests and an analysis synopsis.
        Require digests for nonempty chapters and reuse verified synopsis caches on resume.
        Return empty when book understanding is disabled or optional synopsis synthesis fails.
        """
        if not self._config.pipeline.book_understanding:
            store.log_event("book_understanding_skipped", reason="disabled")
            return ""
        manifest = store.load_manifest()
        chapters = manifest.get("chapters", [])

        loaded = [store.load_chapter(row.get("index", i)) for i, row in enumerate(chapters)]
        digests = self.ensure_chapter_digests(store, loaded, progress)
        if not digests:
            return ""

        analysis = store.load_analysis() or {}
        terms = sorted(store.all_terms(), key=lambda term: term.source)
        style = self._analyzer.style_brief(analysis, terms=terms)
        inputs = {
            "language_policy": self._config.language_policy("analysis").task_fingerprint(
                "book_synopsis"
            ),
            "chapters": [
                [chapter.index, chapter.meta["source_digest"]]
                for chapter in loaded
                if chapter.text_segments
            ],
            "style": style,
            "source_lang": self._config.source_lang,
            "target_lang": self._config.target_lang,
            "glossary": [
                [term.source, term.target, term.type, term.gender, term.note] for term in terms
            ],
            "routing": self._config.llm.model_dump(mode="json"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        metadata = {"version": 2, "inputs_sha256": fingerprint}
        synopsis = analysis.get("book_synopsis", "")
        if (
            isinstance(synopsis, str)
            and synopsis.strip()
            and analysis.get("book_synopsis_meta") == metadata
        ):
            return synopsis
        if progress:
            progress(0, 0, "Generating whole-book synopsis…")
        synopsis = self._synopsizer.book_synopsis(digests, style, glossary=terms)
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

    def ensure_chapter_digests(
        self,
        store: Storage,
        chapters: list[Chapter],
        progress: ProgressFn | None,
    ) -> list[str]:
        """Checkpoint all incurred requests even when prescan cannot finish."""
        try:
            return self._build_chapter_digests(store, chapters, progress)
        finally:
            self._usage.flush(store, scope="prescan")

    def _build_chapter_digests(
        self,
        store: Storage,
        chapters: list[Chapter],
        progress: ProgressFn | None,
    ) -> list[str]:
        """Prescan staged or initialized chapters and return digests in source order."""
        # Persist results on the calling thread, preserving chapter-level resume and source order.
        loaded = {chapter.index: chapter for chapter in chapters}
        sources = {
            ci: "\n".join(s.source for s in ch.text_segments)
            for ci, ch in loaded.items()
            if ch.text_segments
        }
        fingerprint = content_hash(
            [
                self._config.language_policy("analysis").task_fingerprint("chapter_digest"),
                self._config.llm.model_dump(mode="json"),
            ]
        )
        source_hashes = {
            ci: hashlib.sha256(source.encode("utf-8")).hexdigest() for ci, source in sources.items()
        }
        cache_keys = {}
        for ci in sources:
            cache_keys[ci] = (
                "preparation/digests/"
                + content_hash(
                    [
                        _DIGEST_VERSION,
                        source_hashes[ci],
                        fingerprint,
                        self._config.llm.model_dump(mode="json"),
                    ]
                )
                + ".json"
            )
            saved = store.read_artifact(cache_keys[ci])
            if (
                (
                    not _digest_complete(loaded[ci])
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
            if not _digest_complete(loaded[ci])
            or loaded[ci].meta.get("source_digest_policy") != fingerprint
            or loaded[ci].meta.get("source_digest_sha256") != source_hashes[ci]
        ]
        failed: list[int] = []
        error: Exception | None = None
        if todo:
            store.log_event(
                "book_understanding_chapter_digest_started",
                chapters=[ci for ci, _ in todo],
                workers=max(1, self._config.pipeline.prescan_concurrency),
            )
            workers = max(1, self._config.pipeline.prescan_concurrency)
            if progress:
                progress(0, len(todo), "Prescanning chapter digests")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self._synopsizer.digest_chapter, src): ci for ci, src in todo}
                for n_done, fut in enumerate(as_completed(futs), 1):
                    ci = futs[fut]
                    try:
                        digest = fut.result().strip()
                    except Exception as exc:
                        # Drain submitted work and save successes before propagating failures.
                        error = error or exc
                        digest = ""
                    if digest:
                        loaded[ci].meta["source_digest"] = digest
                        loaded[ci].meta["source_digest_complete"] = True
                        loaded[ci].meta["source_digest_policy"] = fingerprint
                        loaded[ci].meta["source_digest_version"] = _DIGEST_VERSION
                        loaded[ci].meta["source_digest_sha256"] = source_hashes[ci]
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
