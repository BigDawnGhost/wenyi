"""Opaque entity-marker transport ("names never pass through the model").

Replaces unambiguous high-stability proper nouns in source text with opaque transport
tokens (``⟦N0⟧``, ``⟦N1⟧``, ...) before translation, supplying a compact grammatical
legend (type + gender) so the model handles pronouns, articles and possessives naturally
while being structurally unable to drift or misspell the entity name. After translation,
every token is deterministically filled from the frozen glossary table.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ...glossary.store import STATUS_OK, raw_source_pattern, source_matches_text
from ...models.glossary import GlossaryTerm

_ENTITY_MARKER_TYPES = frozenset({"person", "place"})
_MARKER_TOKEN_RE = re.compile(r"⟦N(\d+)⟧")


@dataclass(frozen=True)
class EntityMaskPlan:
    masked_sources: tuple[str, ...]
    marker_to_term: dict[str, GlossaryTerm]
    legend_block: str

    @property
    def active(self) -> bool:
        return bool(self.marker_to_term)


def select_markable_terms(
    terms: Sequence[GlossaryTerm],
    sources: Sequence[str],
    *,
    max_markers: int = 24,
) -> list[GlossaryTerm]:
    """Pick unambiguous multi-character person/place terms present in ``sources``."""
    joined = "\n".join(sources)
    candidates: list[GlossaryTerm] = []
    for term in terms:
        if term.status != STATUS_OK:
            continue
        if term.type not in _ENTITY_MARKER_TYPES:
            continue
        src = term.source.strip()
        tgt = term.target.strip()
        if len(src) < 2 or len(tgt) < 2:
            continue
        if not source_matches_text(src, joined):
            continue
        # Only mask unambiguous capitalized entity targets (avoid generic nouns).
        if not any(ch.isupper() for ch in tgt):
            continue
        candidates.append(term)
    # Longest source first so compound names (e.g. 孫悟空) mask before shorter parts (悟空).
    candidates.sort(key=lambda t: (-len(t.source), t.source))
    return candidates[:max_markers]


def mask_batch_entities(
    sources: Sequence[str],
    terms: Sequence[GlossaryTerm],
    *,
    max_markers: int = 24,
) -> EntityMaskPlan:
    """Replace eligible entity occurrences in ``sources`` with ``⟦N0⟧``..``⟦Nk⟧`` tokens."""
    selected = select_markable_terms(terms, sources, max_markers=max_markers)
    if not selected:
        return EntityMaskPlan(tuple(sources), {}, "")

    marker_to_term: dict[str, GlossaryTerm] = {}
    masked_list = list(sources)
    used_markers: list[tuple[str, GlossaryTerm]] = []

    for idx, term in enumerate(selected):
        marker = f"⟦N{idx}⟧"
        pattern = raw_source_pattern(term.source)
        new_list = [pattern.sub(marker, text) for text in masked_list]
        if new_list != masked_list:
            masked_list = new_list
            marker_to_term[marker] = term
            used_markers.append((marker, term))

    if not used_markers:
        return EntityMaskPlan(tuple(sources), {}, "")

    legend_lines = [
        "[Protected entity markers — copy each ⟦N#⟧ token verbatim in the translation; "
        "do not translate, spell out, or alter the brackets. Possessives like ⟦N0⟧'s are allowed. "
        "Do NOT add a leading article ('the') before a bare proper name marker.]"
    ]
    for marker, term in used_markers:
        tgt = term.target.strip()
        takes_article = tgt.lower().startswith("the ") or (
            " " in tgt
            and any(w in tgt.split() for w in ("of", "Emperor", "King", "Sage", "Master"))
        )
        desc = term.type
        if term.gender and term.gender != "unknown":
            desc = f"{desc}, {term.gender}"
        if not takes_article:
            desc = f"{desc}; bare name, no 'the'"
        legend_lines.append(f"- {marker}: {desc}")
    return EntityMaskPlan(
        masked_sources=tuple(masked_list),
        marker_to_term=marker_to_term,
        legend_block="\n".join(legend_lines),
    )


def unmask_batch_entities(
    targets: Sequence[str],
    plan: EntityMaskPlan,
) -> tuple[list[str], int, int]:
    """Deterministically fill every ``⟦N#⟧`` token with its frozen canonical target.

    Returns ``(filled_targets, filled_count, unresolved_count)``. Unresolved counts unknown
    tokens left in a target and every expected marker the model dropped from its segment,
    so a caller can retranslate instead of saving a translation that lost an entity.
    """
    if not plan.active:
        return list(targets), 0, 0

    filled_count = 0
    unresolved_count = 0
    out: list[str] = []
    for index, text in enumerate(targets):
        updated = text
        source = plan.masked_sources[index] if index < len(plan.masked_sources) else ""
        for marker, term in plan.marker_to_term.items():
            hits = updated.count(marker)
            unresolved_count += max(0, source.count(marker) - hits)
            if hits:
                updated = updated.replace(marker, term.target.strip())
                filled_count += hits
        leftovers = _MARKER_TOKEN_RE.findall(updated)
        unresolved_count += len(leftovers)
        out.append(updated)
    return out, filled_count, unresolved_count
