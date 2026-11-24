"""Resumable Book Bible seeding service for compiled glossary mode.

Runs before body translation starts under the book lock. Caches its compiled result as a
Storage artifact keyed by source hash, digest content, synopsis, style brief, existing user
glossary snapshot, language direction and routing fingerprint so resumes never repeat
probe/adjudication model calls. User-imported or manually edited glossary entries
(``first_chapter is None``) are never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence

from ...agents.book_bible import BookBibleAgent, select_voice_excerpts
from ...config import Config
from ...events import ProgressFn
from ...glossary.store import normalize_match_text
from ...llm.routing import inference_snapshot
from ...models.glossary import TYPE_NAVIGATION, GlossaryTerm
from ...storage.protocol import Storage

_LOGGER = logging.getLogger(__name__)
_BIBLE_ARTIFACT = "glossary_bible.json"
_BIBLE_VERSION = 4


def _bible_fingerprint(
    config: Config,
    source_sha256: str,
    digests: Sequence[str],
    book_synopsis: str,
    style: str,
    user_terms: Sequence[GlossaryTerm] = (),
    repeated_quotes: Sequence[str] = (),
) -> str:
    locked_user_terms = sorted(
        (normalize_match_text(t.source).strip(), t.target.strip(), t.type, t.gender)
        for t in user_terms
        if t.first_chapter is None and t.type != TYPE_NAVIGATION
    )
    payload = {
        "version": _BIBLE_VERSION,
        "source_sha256": source_sha256,
        "source_lang": config.source_lang,
        "target_lang": config.target_lang,
        "digests": list(digests),
        "synopsis_hash": hashlib.sha256(book_synopsis.encode("utf-8")).hexdigest(),
        "style_hash": hashlib.sha256(style.encode("utf-8")).hexdigest(),
        "user_terms": locked_user_terms,
        "repeated_quotes": list(repeated_quotes),
        "routing": inference_snapshot(config.llm, ("glossary.extract", "translation.body")),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class BibleSeedService:
    """Compile and persist a frozen pre-translation Book Bible via Storage."""

    def __init__(self, agent: BookBibleAgent, config: Config):
        self._agent = agent
        self._config = config
        self.last_conventions: tuple[str, ...] = ()
        self.last_quote_targets: dict[str, str] = {}
        self.last_voice_exemplars: tuple[tuple[str, str], ...] = ()
        self.last_divergent_titles: dict[str, tuple[str, ...]] = {}

    def ensure_seeded(
        self,
        store: Storage,
        *,
        digests: Sequence[str],
        source_corpus: str,
        book_synopsis: str,
        style: str,
        repeated_quotes: Sequence[str] = (),
        progress: ProgressFn | None = None,
    ) -> dict[str, tuple[str, ...]]:
        """Seed the glossary once before translation; return observed variant spellings."""
        manifest = store.load_manifest()
        source_hash = str(manifest.get("source_sha256") or "")
        existing_terms = [t for t in store.all_terms() if t.type != TYPE_NAVIGATION]
        fp = _bible_fingerprint(
            self._config,
            source_hash,
            digests,
            book_synopsis,
            style,
            user_terms=existing_terms,
            repeated_quotes=repeated_quotes,
        )

        saved = store.read_artifact(_BIBLE_ARTIFACT)
        if isinstance(saved, dict) and saved.get("fingerprint") == fp:
            variants = {
                k: tuple(v)
                for k, v in (saved.get("variants_by_source") or {}).items()
                if isinstance(k, str) and isinstance(v, list)
            }
            self.last_conventions = tuple(
                c for c in (saved.get("conventions") or ()) if isinstance(c, str) and c.strip()
            )
            self.last_quote_targets = {
                k: v
                for k, v in (saved.get("quote_targets") or {}).items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }
            self.last_voice_exemplars = tuple(
                (pair[0], pair[1])
                for pair in (saved.get("voice_exemplars") or ())
                if isinstance(pair, list)
                and len(pair) == 2
                and isinstance(pair[0], str)
                and isinstance(pair[1], str)
                and pair[0].strip()
                and pair[1].strip()
            )
            self.last_divergent_titles = {
                k: tuple(v)
                for k, v in (saved.get("divergent_titles") or {}).items()
                if isinstance(k, str) and isinstance(v, list)
            }
            store.log_event(
                "glossary_bible_reused",
                seeded=len(saved.get("seeded_sources") or []),
                divergent=saved.get("divergent_count", 0),
                conventions=len(self.last_conventions),
                pretranslated_quotes=len(self.last_quote_targets),
                voice_exemplars=len(self.last_voice_exemplars),
            )
            return variants

        if progress:
            progress(0, 0, "progress.stage.compilingBible")

        existing_keys = {normalize_match_text(t.source).strip() for t in existing_terms}
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
            norm_src = normalize_match_text(term.source).strip()
            if norm_src in existing_keys:
                continue
            store.upsert_term(term)
            existing_keys.add(norm_src)
            inserted.append(term.source)

        all_terms = [t for t in store.all_terms() if t.type != TYPE_NAVIGATION]
        quote_targets: dict[str, str] = {}
        if repeated_quotes:
            quote_targets = self._agent.pretranslate_quotes(
                repeated_quotes,
                terms=all_terms,
                style=style,
                conventions=result.conventions,
            )

        voice_excerpts = select_voice_excerpts(
            source_corpus,
            source_lang=self._config.source_lang,
        )
        voice_exemplars: tuple[tuple[str, str], ...] = ()
        if voice_excerpts:
            voice_exemplars = self._agent.compile_voice_exemplars(
                voice_excerpts,
                terms=all_terms,
                style=style,
                conventions=result.conventions,
            )

        self.last_conventions = result.conventions
        self.last_quote_targets = quote_targets
        self.last_voice_exemplars = voice_exemplars
        self.last_divergent_titles = {src: vars_ for src, vars_ in result.divergent_titles}

        record = {
            "version": _BIBLE_VERSION,
            "fingerprint": fp,
            "candidate_count": result.candidate_count,
            "divergent_count": result.divergent_count,
            "seeded_sources": inserted,
            "variants_by_source": {k: list(v) for k, v in result.variants_by_source.items()},
            "conventions": list(result.conventions),
            "quote_targets": quote_targets,
            "voice_exemplars": [[s, t] for s, t in voice_exemplars],
            "divergent_titles": {k: list(v) for k, v in self.last_divergent_titles.items()},
        }
        store.write_artifact(_BIBLE_ARTIFACT, record)
        store.log_event(
            "glossary_bible_compiled",
            candidates=result.candidate_count,
            divergent=result.divergent_count,
            seeded=len(inserted),
            conventions=len(result.conventions),
            pretranslated_quotes=len(quote_targets),
            voice_exemplars=len(voice_exemplars),
        )
        return result.variants_by_source
