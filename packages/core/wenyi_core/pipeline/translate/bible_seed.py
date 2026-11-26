"""Resumable Book Bible seeding service for compiled glossary mode.

Runs before body translation starts under the book lock. Caches its compiled result as a
Storage artifact keyed by source hash, digest content, synopsis, style brief, existing user
glossary snapshot, language direction and routing fingerprint so resumes never repeat
probe/adjudication model calls. The adjudicated result is checkpointed as soon as it is paid
for, and the quote pass before the voice pass, so an interruption at any later step resumes
with the same outcome as an uninterrupted run. Compiled terms stay staged in memory (the quote
and voice passes read them from there) and reach the glossary only once the Bible artifact is
written, through an idempotent publish that a resumed run repeats safely. An unfinished
compilation therefore never changes the glossary a recompile sees. User-imported or manually
edited glossary entries (``first_chapter is None``) are never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, fields, replace
from typing import Any

from ...agents.book_bible import BibleResult, BookBibleAgent, select_voice_excerpts
from ...config import Config
from ...events import ProgressFn
from ...glossary.store import normalize_match_text
from ...llm.routing import inference_snapshot
from ...models.glossary import TYPE_NAVIGATION, GlossaryTerm
from ...storage.protocol import Storage

_LOGGER = logging.getLogger(__name__)
_BIBLE_ARTIFACT = "glossary_bible.json"
# Paid intermediate results of an unfinished compilation; removed once the Bible is complete.
_CHECKPOINT_ARTIFACT = "glossary_bible.checkpoint.json"
_TERM_FIELDS = frozenset(f.name for f in fields(GlossaryTerm))
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


def _term_from_record(item: Any) -> GlossaryTerm | None:
    if not isinstance(item, dict):
        return None
    data = {k: v for k, v in item.items() if k in _TERM_FIELDS}
    if not isinstance(data.get("source"), str) or not isinstance(data.get("target"), str):
        return None
    try:
        return GlossaryTerm(**data)
    except TypeError:
        return None


def _seed_terms_from_record(saved: Any) -> list[GlossaryTerm]:
    raw = saved.get("seed_terms") if isinstance(saved, dict) else None
    if not isinstance(raw, list):
        return []
    return [term for term in (_term_from_record(item) for item in raw) if term is not None]


def _str_tuple_map(raw: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        return {}
    return {
        k: tuple(x for x in v if isinstance(x, str))
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, list)
    }


def _checkpoint_record(
    fingerprint: str, result: BibleResult, seeded_sources: Sequence[str]
) -> dict[str, Any]:
    return {
        "version": _BIBLE_VERSION,
        "fingerprint": fingerprint,
        "terms": [asdict(term) for term in result.terms],
        "seeded_sources": list(seeded_sources),
        "candidate_count": result.candidate_count,
        "divergent_count": result.divergent_count,
        "variants_by_source": {k: list(v) for k, v in result.variants_by_source.items()},
        "conventions": list(result.conventions),
        "divergent_titles": [[src, list(vars_)] for src, vars_ in result.divergent_titles],
    }


def _result_from_checkpoint(saved: Any, fingerprint: str) -> tuple[BibleResult, list[str]] | None:
    """Decode a checkpoint for ``fingerprint``; ``None`` when absent, stale or malformed."""
    if not isinstance(saved, dict) or saved.get("fingerprint") != fingerprint:
        return None
    raw_terms = saved.get("terms")
    raw_seeded = saved.get("seeded_sources")
    raw_titles = saved.get("divergent_titles")
    if not isinstance(raw_terms, list) or not isinstance(raw_seeded, list):
        return None
    terms = [_term_from_record(item) for item in raw_terms]
    if any(term is None for term in terms) or not isinstance(raw_titles, list):
        return None
    titles = tuple(
        (pair[0], tuple(v for v in pair[1] if isinstance(v, str)))
        for pair in raw_titles
        if isinstance(pair, list)
        and len(pair) == 2
        and isinstance(pair[0], str)
        and isinstance(pair[1], list)
    )
    result = BibleResult(
        terms=tuple(t for t in terms if t is not None),
        variants_by_source=_str_tuple_map(saved.get("variants_by_source")),
        candidate_count=int(saved.get("candidate_count") or 0),
        divergent_count=int(saved.get("divergent_count") or 0),
        conventions=tuple(
            c for c in (saved.get("conventions") or ()) if isinstance(c, str) and c.strip()
        ),
        divergent_titles=titles,
    )
    return result, [s for s in raw_seeded if isinstance(s, str)]


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
        checkpoint_usage: Callable[[], None] | None = None,
    ) -> dict[str, tuple[str, ...]]:
        """Seed the glossary once before translation; return observed variant spellings.

        ``checkpoint_usage`` persists the usage of the model calls made so far; it runs before
        each checkpoint is written, so a hard stop never leaves a paid call unrecorded.
        """
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
            # A leftover checkpoint means a run stopped after the artifact but before publication
            # finished; complete it once. Otherwise publication is done, and later glossary edits
            # (a deleted seed, a resolved conflict) belong to the user.
            # A checkpoint for other inputs holds paid work a later run may still reuse.
            leftover = store.read_artifact(_CHECKPOINT_ARTIFACT)
            if isinstance(leftover, dict) and leftover.get("fingerprint") == fp:
                self._publish_seeds(store, _seed_terms_from_record(saved))
                store.delete_artifact(_CHECKPOINT_ARTIFACT)
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

        checkpoint = store.read_artifact(_CHECKPOINT_ARTIFACT)
        restored = _result_from_checkpoint(checkpoint, fp)
        if restored is not None:
            result, inserted = restored
            store.log_event("glossary_bible_checkpoint_reused", seeded=len(inserted))
        else:
            result, inserted = self._compile(
                digests,
                source_corpus,
                book_synopsis=book_synopsis,
                style=style,
                existing_terms=existing_terms,
            )
            if checkpoint_usage is not None:
                checkpoint_usage()
            checkpoint = _checkpoint_record(fp, result, inserted)
            store.write_artifact(_CHECKPOINT_ARTIFACT, checkpoint)

        # Staged seeds: the quote and voice passes see the glossary as publication will leave it,
        # without the glossary changing yet. A seed over a navigation stub with another target
        # only opens a conflict, so the prompts do not see it.
        pending = set(inserted)
        seeds = [t for t in result.terms if t.source in pending]
        present = {normalize_match_text(t.source).strip() for t in existing_terms}
        # Navigation stubs are matched by exact source, as ``upsert_term`` matches them.
        nav_targets = {
            t.source: t.target.strip() for t in store.all_terms() if t.type == TYPE_NAVIGATION
        }
        all_terms = list(existing_terms) + [
            t
            for t in seeds
            if normalize_match_text(t.source).strip() not in present
            and nav_targets.get(t.source, t.target.strip()) == t.target.strip()
        ]
        # Saved quote renderings are reused only for the same prompt glossary: a navigation edit
        # leaves the fingerprint unchanged but can change which seeds the prompts see.
        terms_key = hashlib.sha256(
            json.dumps(
                sorted((t.source, t.target, t.type) for t in all_terms), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        quote_targets: dict[str, str] = {}
        saved_quotes = checkpoint.get("quote_targets") if isinstance(checkpoint, dict) else None
        if isinstance(saved_quotes, dict) and checkpoint.get("quote_terms_key") == terms_key:
            quote_targets = {
                k: v
                for k, v in saved_quotes.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }
        elif repeated_quotes:
            quote_targets = self._agent.pretranslate_quotes(
                repeated_quotes,
                terms=all_terms,
                style=style,
                conventions=result.conventions,
            )
            if checkpoint_usage is not None:
                checkpoint_usage()
            # Keep the paid quote renderings if the voice pass is interrupted next.
            checkpoint = {
                **checkpoint,
                "quote_targets": quote_targets,
                "quote_terms_key": terms_key,
            }
            store.write_artifact(_CHECKPOINT_ARTIFACT, checkpoint)

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
            "seed_terms": [asdict(term) for term in seeds],
            "variants_by_source": {k: list(v) for k, v in result.variants_by_source.items()},
            "conventions": list(result.conventions),
            "quote_targets": quote_targets,
            "voice_exemplars": [[s, t] for s, t in voice_exemplars],
            "divergent_titles": {k: list(v) for k, v in self.last_divergent_titles.items()},
        }
        if checkpoint_usage is not None:
            checkpoint_usage()
        store.write_artifact(_BIBLE_ARTIFACT, record)
        self._publish_seeds(store, seeds)
        store.delete_artifact(_CHECKPOINT_ARTIFACT)
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

    @staticmethod
    def _publish_seeds(store: Storage, seeds: Sequence[GlossaryTerm]) -> None:
        """Write staged Bible terms to the glossary; repeating it changes nothing.

        A source that already has a non-navigation entry (compared by normalized match key) is
        kept as is. A navigation stub is upserted once: if that records a conflict, a repeat
        finds the open conflict and stops.
        """
        if not seeds:
            return
        conflicts = {
            (
                normalize_match_text(str(c.get("source") or "")).strip(),
                str(c.get("proposed_target") or "").strip(),
            )
            for c in store.open_conflicts()
        }
        entries = {
            normalize_match_text(t.source).strip()
            for t in store.all_terms()
            if t.type != TYPE_NAVIGATION
        }
        for term in seeds:
            norm = normalize_match_text(term.source).strip()
            if norm in entries or (norm, term.target.strip()) in conflicts:
                continue
            store.upsert_term(term)
            entries.add(norm)

    def _compile(
        self,
        digests: Sequence[str],
        source_corpus: str,
        *,
        book_synopsis: str,
        style: str,
        existing_terms: Sequence[GlossaryTerm],
    ) -> tuple[BibleResult, list[str]]:
        """Run the paid probe/adjudication and choose which compiled terms to seed."""
        max_candidates = min(260, max(120, len(digests) * 12))
        result = self._agent.compile_bible(
            digests,
            source_corpus,
            book_synopsis=book_synopsis,
            style=style,
            existing_terms=existing_terms,
            max_candidates=max_candidates,
        )
        # Seeded terms must never look like user entries: those alone shape the fingerprint.
        terms = tuple(
            replace(t, first_chapter=0) if t.first_chapter is None else t for t in result.terms
        )
        result = replace(result, terms=terms)
        existing_keys = {normalize_match_text(t.source).strip() for t in existing_terms}
        inserted: list[str] = []
        for term in terms:
            norm_src = normalize_match_text(term.source).strip()
            if norm_src in existing_keys:
                continue
            existing_keys.add(norm_src)
            inserted.append(term.source)
        return result, inserted
