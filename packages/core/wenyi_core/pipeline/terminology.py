"""Book-wide terminology admission and pretranslation evidence checkpoints.

Model calls run outside state transactions. Immutable request results are cached through
the artifact port; a recoverable publication record precedes each glossary update.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from typing import Any

from ..agents.terminology import (
    TerminologyAgent,
    TerminologyKnowledgeError,
    TerminologyReferenceError,
)
from ..glossary.evidence import BookSourceIndex
from ..glossary.evidence_models import (
    DiscoveryRejection,
    DiscoveryResult,
    KnowledgeResult,
    SourcePassage,
)
from ..glossary.extractor import GlossaryExtractor, TranslatedSegmentEvidence
from ..glossary.store import GlossaryTerm, source_matches_text
from ..i18n.policy.models import content_hash
from ..ingest.models import Chapter, Segment
from ..storage.protocol import Storage
from .terminology_metadata import protected_metadata_fields

EVIDENCE_OPERATIONS = ("terminology.context",)
# Evidence-only response contracts must not reuse mixed-experiment request caches.
_VERSION = 4
logger = logging.getLogger(__name__)


class TerminologyService:
    """Use source evidence without rewriting previously translated paragraphs."""

    def __init__(
        self,
        store: Storage,
        agent: TerminologyAgent,
        chapters: list[Chapter],
        *,
        checkpoint_usage: Callable[[], Any] | None = None,
    ):
        self.store = store
        self.agent = agent
        self.index = BookSourceIndex(chapters)
        self.checkpoint_usage = checkpoint_usage
        self.identity = {
            "version": _VERSION,
            "source": self.index.fingerprint,
            "policy": agent.language_policy.fingerprint,
            "routing": content_hash(agent.config.llm.model_dump(mode="json")),
        }
        self._records: dict[str, dict[str, Any]] = {}
        for key in store.list_artifacts("terminology/entries/"):
            record = store.read_artifact(key)
            if (
                isinstance(record, dict)
                and record.get("identity") == self.identity
                and record.get("status") == "published"
            ):
                current = store.get_term(record["source"])
                if current is not None and record.get("binding") == self._binding(current):
                    self._records[record["source"]] = record

    @staticmethod
    def _binding(term: GlossaryTerm) -> dict[str, Any]:
        """Only current identity decisions can authorize cached source evidence."""
        return {
            "source": term.source,
            "target": term.target,
            "type": term.type,
            "aliases": sorted(term.aliases),
            "status": term.status,
        }

    def _current_record(self, source: str) -> dict[str, Any] | None:
        record = self._records.get(source)
        current = self.store.get_term(source)
        return (
            record
            if record is not None
            and current is not None
            and record.get("binding") == self._binding(current)
            else None
        )

    @classmethod
    def for_store(
        cls, store: Storage, agent: TerminologyAgent, **kwargs: Any
    ) -> TerminologyService:
        chapters = [
            store.load_chapter(row["index"]) for row in store.load_manifest().get("chapters", [])
        ]
        return cls(store, agent, chapters, **kwargs)

    def _request(
        self, kind: str, inputs: Any, invoke: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        key = f"terminology/requests/{kind}/{content_hash([self.identity, inputs])}.json"
        saved = self.store.read_artifact(key)
        if isinstance(saved, dict):
            return saved
        try:
            result = invoke()
            if kind == "discover" and result.get("rejections"):
                diagnostic_key = key.replace("terminology/requests/", "terminology/diagnostics/", 1)
                rejections = [
                    DiscoveryRejection.model_validate(item, strict=True).model_dump()
                    for item in result["rejections"]
                ]
                diagnostic = {
                    "schema_version": 1,
                    "request_key": key,
                    "operation": "terminology.discover",
                    "reason": "rejected_proposals",
                    "accepted_terms_count": len(result["terms"]),
                    "rejected_count": len(rejections),
                    "rejections": rejections,
                }
                # Cache only accepted proposals; diagnostics retain no rejected model values.
                self.store.write_artifact(diagnostic_key, diagnostic)
                self.store.log_event(
                    "terminology_discovery_proposals_rejected",
                    diagnostic_key=diagnostic_key,
                    **diagnostic,
                )
                logger.warning(
                    "Rejected %d terminology discovery proposals (accepted_terms=%d); see %s",
                    len(rejections),
                    len(result["terms"]),
                    diagnostic_key,
                )
            self.store.write_artifact(key, result)
            return result
        except (TerminologyReferenceError, TerminologyKnowledgeError) as error:
            diagnostic_key = key.replace("terminology/requests/", "terminology/diagnostics/", 1)
            diagnostic = {
                "schema_version": 1,
                "request_key": key,
                **error.diagnostic(),
            }
            # Failed responses never become reusable evidence. Record only schema paths,
            # codes and counts, not source, model prose or arbitrary response properties.
            self.store.write_artifact(diagnostic_key, diagnostic)
            self.store.log_event(
                (
                    "terminology_reference_validation_failed"
                    if isinstance(error, TerminologyReferenceError)
                    else "terminology_knowledge_validation_failed"
                ),
                diagnostic_key=diagnostic_key,
                **diagnostic,
            )
            raise
        finally:
            if self.checkpoint_usage is not None:
                self.checkpoint_usage()

    def _discover(self, passages: list[SourcePassage]) -> DiscoveryResult:
        existing = self.store.all_terms()

        def invoke() -> dict[str, Any]:
            result = self.agent.discover(passages, existing)
            return {
                "terms": [asdict(term) for term in result.terms],
                "rejections": [rejection.model_dump() for rejection in result.rejections],
            }

        # Canonicalization depends on the live glossary. Its changes invalidate discovery,
        # including when an editor removes or reassigns an alias.
        result = self._request(
            "discover",
            [[p.model_dump() for p in passages], [asdict(term) for term in existing]],
            invoke,
        )
        return DiscoveryResult(
            terms=[GlossaryTerm(**term) for term in result["terms"]],
            rejections=[
                DiscoveryRejection.model_validate(item) for item in result.get("rejections", [])
            ],
        )

    def _knowledge(self, term: GlossaryTerm) -> tuple[KnowledgeResult, dict[str, list[int]]] | None:
        groups = self.index.evidence_groups(term, self.store.all_terms())
        if not groups:
            return None
        results = []
        locations: dict[str, list[int]] = {}
        term_key = asdict(term)

        # Include every matching group; there is no sampled-success state.
        for group in groups:
            for passage in group:
                locations[passage.reference] = [
                    passage.chapter,
                    passage.segment,
                    passage.start,
                    len(passage.source),
                ]
            raw = self._request(
                "evidence",
                [term_key, [p.model_dump() for p in group]],
                lambda group=group: self.agent.enrich(term, group).model_dump(),
            )
            result = KnowledgeResult.model_validate(raw)
            results.append(result)
        # An empty, valid group means no supported facts in that portion of the source,
        # not a failed request. All-empty evidence cannot produce a formal glossary entry.
        if not any(
            result.note.strip() or result.reading.strip() or result.gender.strip() or result.aliases
            for result in results
        ):
            return None
        # Pairwise merging keeps request sizes bounded. Each merge is independently
        # reusable; a failed group or merge never publishes partial knowledge.
        while len(results) > 1:
            merged = []
            for start in range(0, len(results), 2):
                group = results[start : start + 2]
                if len(group) == 1:
                    merged.append(group[0])
                    continue
                raw = self._request(
                    "merge",
                    [term_key, [item.model_dump() for item in group]],
                    lambda group=group: self.agent.merge(term, group).model_dump(),
                )
                merged.append(KnowledgeResult.model_validate(raw))
            results = merged
        return results[0], locations

    def admit(self, candidate: GlossaryTerm, chapter: int | None = None) -> str:
        """Enrich a candidate before admission, preserving existing and edited fields."""
        existing = self.store.get_term(candidate.source)
        term = GlossaryTerm(
            source=candidate.source,
            target=existing.target if existing else candidate.target,
            type=existing.type if existing else candidate.type,
            # Only stored aliases are confirmed identities. New model aliases remain
            # proposals in the evidence artifact until an editor confirms them.
            aliases=list(existing.aliases) if existing else [],
            status=existing.status if existing else "ok",
        )
        knowledge = self._knowledge(term)
        if knowledge is None:
            return "unresolved"
        result, locations = knowledge
        key = f"terminology/entries/{content_hash(candidate.source)}.json"
        with self.store.state_lock():
            current = self.store.get_term(candidate.source)
            before = self._binding(existing) if existing is not None else None
            after = self._binding(current) if current is not None else None
            if after != before:
                raise ValueError(
                    "The glossary identity changed during evidence collection. "
                    "Retry to use the updated entry; saved translations are unchanged."
                )
            old = self.store.read_artifact(key)
            old = old if isinstance(old, dict) else {}
            if old.get("identity", {}).get("source") != self.index.fingerprint:
                old = {}
            protected = protected_metadata_fields(current, old)
            # If a field differs from the last publication, preserve the edit. Existing
            # nonempty fields without provenance are conservatively treated as manual.
            values = {}
            for field in ("note", "reading", "gender"):
                value = getattr(current, field) if current else ""
                values[field] = value if field in protected else getattr(result, field) or value
            aliases = list(current.aliases) if current is not None else []
            first = next(
                (
                    passage.chapter
                    for passage in self.index.passages
                    if source_matches_text(candidate.source, passage.source)
                ),
                chapter,
            )
            publication = replace(
                term,
                target=current.target if current else term.target,
                aliases=aliases,
                first_chapter=current.first_chapter if current else first,
                **values,
            )
            record = {
                "identity": self.identity,
                "source": candidate.source,
                "binding": self._binding(term),
                "knowledge": result.model_dump(),
                "locations": locations,
                "publication": asdict(publication),
                "previous": asdict(current) if current else None,
                "protected_fields": sorted(protected),
                "status": "prepared",
            }
            # The recoverable index comes first. Replaying the same-target update is
            # idempotent; targets changed by an editor remain authoritative.
            self.store.write_artifact(key, record)
            outcome = self.store.upsert_term(publication, chapter=chapter)
            if current is not None and candidate.target != current.target:
                # Keep the rejected mapping visible without replacing any metadata.
                recorded = any(
                    conflict["source"] == candidate.source
                    and conflict["existing_target"] == current.target
                    and conflict["proposed_target"] == candidate.target
                    for conflict in self.store.open_conflicts()
                )
                if not recorded:
                    self.store.upsert_term(
                        GlossaryTerm(candidate.source, candidate.target), chapter=chapter
                    )
                outcome = "conflict"
            published = self.store.get_term(candidate.source)
            if published is None:
                raise ValueError(
                    "The glossary entry disappeared during publication. Retry admission."
                )
            record["binding"] = self._binding(published)
            record["status"] = "published"
            self.store.write_artifact(key, record)
            self._records[candidate.source] = record
        return outcome

    def prepare_batch(
        self,
        chapter: int,
        segments: list[Segment],
        *,
        extractor: GlossaryExtractor,
        history: Iterable[TranslatedSegmentEvidence] = (),
        before: tuple[int, int] | None = None,
    ) -> None:
        """Discover and admit source-attested terms before translation."""
        passages = self.index.for_segments(chapter, segments)
        discovery = self._discover(passages)
        candidates = discovery.terms
        occurrences = (
            extractor._first_occurrences(candidates, self.store, history, before)
            if before is not None
            else {}
        )
        terms, _, _ = extractor._align_with_first_occurrences(
            candidates, occurrences, store=self.store
        )
        for term in terms:
            self.admit(term, chapter)
        # Discovery may legitimately omit already known terms. Refresh source-relevant
        # entries whose naming/alias decisions changed since their evidence was collected.
        relevant = self.store.terms_in(
            self.store.all_terms(), "\n".join(segment.source for segment in segments)
        )
        for term in relevant:
            if self._current_record(term.source) is None:
                self.admit(term, chapter)
