"""Resumable Book Bible seeding service for compiled glossary mode.

Runs before body translation starts under the book lock. Caches its compiled result as a
Storage artifact keyed by source hash, digest content, synopsis, style brief, language
direction and routing fingerprint so resumes never repeat probe/adjudication model calls. User-imported or
manually edited glossary entries (``first_chapter is None``) are never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence

from ...agents.book_bible import BookBibleAgent
from ...config import Config
from ...events import ProgressFn
from ...llm.routing import inference_snapshot
from ...storage.protocol import Storage

_LOGGER = logging.getLogger(__name__)
_BIBLE_ARTIFACT = "glossary_bible.json"
_BIBLE_VERSION = 2


def _bible_fingerprint(
    config: Config,
    source_sha256: str,
    digests: Sequence[str],
    book_synopsis: str,
    style: str,
) -> str:
    payload = {
        "version": _BIBLE_VERSION,
        "source_sha256": source_sha256,
        "source_lang": config.source_lang,
        "target_lang": config.target_lang,
        "digests": list(digests),
        "synopsis_hash": hashlib.sha256(book_synopsis.encode("utf-8")).hexdigest(),
        # The adjudicator reads the style brief, so a new brief must recompile.
        "style_hash": hashlib.sha256(style.encode("utf-8")).hexdigest(),
        "routing": inference_snapshot(config.llm, ("glossary.extract",)),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class BibleSeedService:
    """Compile and persist a frozen pre-translation Book Bible via Storage."""

    def __init__(self, agent: BookBibleAgent, config: Config):
        self._agent = agent
        self._config = config

    def ensure_seeded(
        self,
        store: Storage,
        *,
        digests: Sequence[str],
        source_corpus: str,
        book_synopsis: str,
        style: str,
        progress: ProgressFn | None = None,
    ) -> dict[str, tuple[str, ...]]:
        """Seed the glossary once before translation; return observed variant spellings."""
        manifest = store.load_manifest()
        source_hash = str(manifest.get("source_sha256") or "")
        fp = _bible_fingerprint(self._config, source_hash, digests, book_synopsis, style)

        saved = store.read_artifact(_BIBLE_ARTIFACT)
        if isinstance(saved, dict) and saved.get("fingerprint") == fp:
            variants = {
                k: tuple(v)
                for k, v in (saved.get("variants_by_source") or {}).items()
                if isinstance(k, str) and isinstance(v, list)
            }
            store.log_event(
                "glossary_bible_reused",
                seeded=len(saved.get("seeded_sources") or []),
                divergent=saved.get("divergent_count", 0),
            )
            return variants

        if progress:
            progress(0, 0, "Compiling Book Bible (entropy probe)…")

        existing_terms = store.all_terms()
        existing_keys = {t.source for t in existing_terms}
        max_candidates = min(260, max(120, len(digests) * 12))
        result = self._agent.compile_bible(
            digests,
            source_corpus,
            book_synopsis=book_synopsis,
            style=style,
            existing_terms=existing_terms,
            max_candidates=max_candidates,
        )

        inserted: list[str] = []
        for term in result.terms:
            if term.source in existing_keys:
                continue
            store.upsert_term(term)
            inserted.append(term.source)

        record = {
            "version": _BIBLE_VERSION,
            "fingerprint": fp,
            "candidate_count": result.candidate_count,
            "divergent_count": result.divergent_count,
            "seeded_sources": inserted,
            "variants_by_source": {k: list(v) for k, v in result.variants_by_source.items()},
        }
        store.write_artifact(_BIBLE_ARTIFACT, record)
        store.log_event(
            "glossary_bible_compiled",
            candidates=result.candidate_count,
            divergent=result.divergent_count,
            seeded=len(inserted),
        )
        return result.variants_by_source
