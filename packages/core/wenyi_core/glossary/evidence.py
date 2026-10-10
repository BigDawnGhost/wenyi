"""Pure, exhaustive source indexing for terminology evidence."""

from __future__ import annotations

import hashlib
import json

from ..ingest.models import Chapter, Segment
from .evidence_models import SourcePassage
from .store import GlossaryTerm, _match_text, source_matches_text, term_match_sources


def trusted_match_sources(term: GlossaryTerm, all_terms: list[GlossaryTerm]) -> list[str]:
    """Use approved aliases only when no other canonical identity claims them.

    Existing glossary aliases with status ``ok`` are trusted identity assertions.
    Conflicted entries and aliases shared with any other identity are not.
    Matching normalization is deliberately identical to glossary matching.
    """
    canonical = _match_text(term.source).strip()
    claimed = {
        _match_text(key).strip()
        for other in all_terms
        if _match_text(other.source).strip() != canonical
        for key in [other.source, *other.aliases]
    }
    return [
        key
        for key in dict.fromkeys(term_match_sources(term))
        if key == term.source or (term.status == "ok" and _match_text(key).strip() not in claimed)
    ]


def _passage(chapter: int, segment: int, source: str, start: int = 0) -> SourcePassage:
    return SourcePassage(
        reference=f"c{chapter}:s{segment}:{start}-{start + len(source)}",
        chapter=chapter,
        segment=segment,
        source=source,
        start=start,
    )


class BookSourceIndex:
    """Snapshot sources in document order, without reading targets or external state."""

    def __init__(self, chapters: list[Chapter]):
        self.passages = [
            _passage(chapter.index, segment.index, segment.source)
            for chapter in chapters
            for segment in chapter.segments
        ]
        self._by_position = {(p.chapter, p.segment): p for p in self.passages}
        if len(self._by_position) != len(self.passages):
            raise ValueError("Source index requires unique chapter/segment identities")
        payload = [(p.chapter, p.segment, p.source) for p in self.passages]
        self.fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def for_segments(self, chapter: int, segments: list[Segment]) -> list[SourcePassage]:
        """Return indexed sources in book order; reject stale or unknown input."""
        wanted = set()
        for segment in segments:
            key = (chapter, segment.index)
            passage = self._by_position.get(key)
            if passage is None or passage.source != segment.source:
                raise ValueError("Segments do not match the source index")
            wanted.add(key)
        return [p for p in self.passages if (p.chapter, p.segment) in wanted]

    def evidence_groups(
        self, term: GlossaryTerm, all_terms: list[GlossaryTerm], budget: int = 12000
    ) -> list[list[SourcePassage]]:
        """Cover all matching paragraphs and adjacent same-chapter context.

        ``budget`` bounds source characters, not serialized prompt tokens. Split
        oversized paragraphs losslessly at character offsets. References include
        both endpoints, so a reference always identifies the same source slice.
        No hit is sampled away; callers must process every returned group.
        """
        if budget <= 0:
            raise ValueError("Evidence budget must be positive")
        keys = trusted_match_sources(term, all_terms)
        selected: set[int] = set()
        for i, passage in enumerate(self.passages):
            if any(source_matches_text(key, passage.source) for key in keys):
                selected.add(i)
                for neighbor in (i - 1, i + 1):
                    if (
                        0 <= neighbor < len(self.passages)
                        and self.passages[neighbor].chapter == passage.chapter
                    ):
                        selected.add(neighbor)
        groups: list[list[SourcePassage]] = []
        group: list[SourcePassage] = []
        size = 0
        for i in sorted(selected):
            passage = self.passages[i]
            for start in range(0, len(passage.source), budget):
                part = _passage(
                    passage.chapter, passage.segment, passage.source[start : start + budget], start
                )
                if group and size + len(part.source) > budget:
                    groups.append(group)
                    group, size = [], 0
                group.append(part)
                size += len(part.source)
        if group:
            groups.append(group)
        return groups
