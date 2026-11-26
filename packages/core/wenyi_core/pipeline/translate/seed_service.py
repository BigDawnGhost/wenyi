"""Resumable upfront CJK glossary seeding service for parallel chapter translation.

Caches the compiled seed as a single atomic Storage artifact keyed by source hash,
digests, synopsis/style, locked user glossary, language direction, and routing
fingerprint. Publishing seeded terms into the glossary store is idempotent and never
overwrites user-imported or manually edited entries (``first_chapter is None``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from ...agents.cjk_seed import CjkGlossarySeeder, mine_cjk_candidates
from ...events import ProgressFn
from ...i18n.languages import glossary_context_dependent_sources
from ...llm.routing import inference_snapshot
from ...models.glossary import TYPE_NAVIGATION, GlossaryTerm
from ...storage.protocol import Storage

if TYPE_CHECKING:
    from ..run.runtime import PipelineRuntime

SEED_ARTIFACT_KEY = "glossary/cjk_seed.json"
_SEED_SCHEMA_VERSION = 1
_MAX_INCOMPLETE_SEED_ATTEMPTS = 2


class CjkGlossarySeedService:
    """Seed a compact, frequency-ordered glossary before parallel chapter translation."""

    def __init__(self, runtime: PipelineRuntime, seeder: CjkGlossarySeeder | None = None):
        self._runtime = runtime
        self._seeder = seeder or CjkGlossarySeeder(runtime.client, runtime.config)

    def ensure_seeded(
        self,
        store: Storage,
        *,
        digests: Sequence[str],
        source_corpus: str,
        book_synopsis: str = "",
        style: str = "",
        progress: ProgressFn | None = None,
        checkpoint_usage: Callable[[], None] | None = None,
    ) -> int:
        """Seed or restore upfront CJK glossary terms; return count of seeded terms."""
        self._seeder.src = self._runtime.config.source_lang
        self._seeder.tgt = self._runtime.config.target_lang
        self._seeder.language_policy = self._runtime.config.language_policy("translation")

        existing_all = store.all_terms()
        existing_all_sources = {t.source for t in existing_all}
        # Only user-owned non-navigation entries (first_chapter is None) define the seed prefix
        # and cache fingerprint; EPUB chapter titles stay scoped to matching batches.
        user_terms = [
            t for t in existing_all if t.first_chapter is None and t.type != TYPE_NAVIGATION
        ]
        fingerprint = self._fingerprint(store, digests, book_synopsis, style, user_terms)

        cached = store.read_artifact(SEED_ARTIFACT_KEY)
        # Preserve the history of previously published seed sources across fingerprint changes
        # so terms explicitly deleted by the user are never resurrected after adding another term.
        previously_published: set[str] = set()
        if isinstance(cached, dict) and cached.get("version") == _SEED_SCHEMA_VERSION:
            raw_hist = cached.get("published_sources")
            if isinstance(raw_hist, list):
                previously_published = {str(s) for s in raw_hist if isinstance(s, str)}
            elif cached.get("published") is True and isinstance(cached.get("terms"), list):
                previously_published = {
                    str(item["source"])
                    for item in cached["terms"]
                    if isinstance(item, dict) and isinstance(item.get("source"), str)
                }
        user_deleted = previously_published - existing_all_sources

        same_fingerprint = (
            isinstance(cached, dict)
            and cached.get("version") == _SEED_SCHEMA_VERSION
            and cached.get("fingerprint") == fingerprint
            and isinstance(cached.get("terms"), list)
        )
        prior_attempts = (
            int(cached.get("attempts", 0)) if same_fingerprint and isinstance(cached, dict) else 0
        )
        if same_fingerprint and isinstance(cached, dict):
            # Once published (either complete or finalized after max retries), never re-insert
            # on subsequent runs so user-deleted seed terms stay deleted.
            if cached.get("published") is True:
                return 0
            if (
                cached.get("complete", True) is True
                or prior_attempts >= _MAX_INCOMPLETE_SEED_ATTEMPTS
            ):
                terms = [self._deserialize_term(item) for item in cached["terms"]]
                terms = [t for t in terms if t is not None and t.source not in user_deleted]
                published = self._publish_idempotent(store, terms)
                all_published = sorted(previously_published | {t.source for t in terms})
                store.write_artifact(
                    SEED_ARTIFACT_KEY,
                    {**cached, "published": True, "published_sources": all_published},
                )
                return published

        if progress:
            progress(0, 1, "progress.stage.seedingGlossary")

        context_sources = glossary_context_dependent_sources(self._runtime.config.source_lang)
        candidates = mine_cjk_candidates(
            digests,
            source_corpus,
            existing_sources={t.source for t in user_terms} | user_deleted,
            context_dependent_sources=context_sources,
        )
        result = self._seeder.seed_result(
            candidates,
            book_synopsis=book_synopsis,
            style=style,
            existing_terms=user_terms,
            max_workers=max(1, self._runtime.config.pipeline.prescan_concurrency),
        )
        if checkpoint_usage is not None:
            checkpoint_usage()

        seeded = [t for t in result.terms if t.source not in user_deleted]
        attempts = prior_attempts + 1
        finalized = result.complete or attempts >= _MAX_INCOMPLETE_SEED_ATTEMPTS
        if finalized:
            # Persist the paid seed artifact before publishing into SQLite so an interrupt
            # during publication resumes with zero extra LLM calls, then mark it published.
            artifact = {
                "version": _SEED_SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "complete": result.complete,
                "attempts": attempts,
                "terms": [self._serialize_term(t) for t in seeded],
                "published": False,
                "published_sources": sorted(previously_published),
            }
            store.write_artifact(SEED_ARTIFACT_KEY, artifact)
            published = self._publish_idempotent(store, seeded)
            all_published = sorted(previously_published | {t.source for t in seeded})
            store.write_artifact(
                SEED_ARTIFACT_KEY,
                {**artifact, "published": True, "published_sources": all_published},
            )
        else:
            # Partial chunk failure on first attempt: publish valid terms and record the
            # attempt count so one retry runs without re-paying forever on deterministic 400s.
            published = self._publish_idempotent(store, seeded)
            all_published = sorted(previously_published | {t.source for t in seeded})
            store.write_artifact(
                SEED_ARTIFACT_KEY,
                {
                    "version": _SEED_SCHEMA_VERSION,
                    "fingerprint": fingerprint,
                    "complete": False,
                    "attempts": attempts,
                    "terms": [self._serialize_term(t) for t in seeded],
                    "published": False,
                    "published_sources": all_published,
                },
            )
        store.log_event(
            "cjk_glossary_seeded",
            candidates=len(candidates),
            seeded=len(seeded),
            published=published,
            complete=result.complete,
        )
        return published

    def _fingerprint(
        self,
        store: Storage,
        digests: Sequence[str],
        book_synopsis: str,
        style: str,
        user_terms: Sequence[GlossaryTerm],
    ) -> str:
        manifest = store.load_manifest()
        payload = {
            "v": _SEED_SCHEMA_VERSION,
            "source_sha256": manifest.get("source_sha256", ""),
            "src": self._runtime.config.source_lang,
            "tgt": self._runtime.config.target_lang,
            "digests": list(digests),
            "synopsis": book_synopsis,
            "style": style,
            "routing": inference_snapshot(self._runtime.config.llm, ("glossary.extract",)),
            "user_terms": [
                (t.source, t.target, t.type, t.gender, tuple(t.aliases)) for t in user_terms
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _publish_idempotent(store: Storage, terms: Sequence[GlossaryTerm]) -> int:
        """Insert seeded terms in frequency order without overwriting existing entries."""
        existing = {t.source for t in store.all_terms()}
        inserted = 0
        for term in terms:
            if term.source in existing:
                continue
            status = store.upsert_term(term, chapter=0)
            if status == "inserted":
                inserted += 1
            existing.add(term.source)
        return inserted

    @staticmethod
    def _serialize_term(term: GlossaryTerm) -> dict[str, Any]:
        return {
            "source": term.source,
            "target": term.target,
            "type": term.type,
            "gender": term.gender,
            "aliases": list(term.aliases),
        }

    @staticmethod
    def _deserialize_term(raw: Any) -> GlossaryTerm | None:
        if not isinstance(raw, dict):
            return None
        source = str(raw.get("source") or "").strip()
        target = str(raw.get("target") or "").strip()
        if not source or not target:
            return None
        aliases = raw.get("aliases")
        return GlossaryTerm(
            source=source,
            target=target,
            type=str(raw.get("type") or "term"),
            gender=str(raw.get("gender") or ""),
            aliases=[str(a) for a in aliases if isinstance(a, str)]
            if isinstance(aliases, list)
            else [],
            first_chapter=0,
        )
