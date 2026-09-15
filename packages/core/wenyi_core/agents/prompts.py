"""Format glossary, annotation and segment payloads for agent prompts."""

from __future__ import annotations

import json

from ..glossary.store import GlossaryTerm


def render_glossary(terms: list[GlossaryTerm]) -> str:
    """Render glossary objects as a line-by-line reference for prompts."""
    if not terms:
        return "(none)"
    lines = []
    for t in terms:
        extra = []
        if t.gender:
            extra.append(t.gender)
        if t.reading:
            extra.append(f"Pronunciation: {t.reading}")
        tag = f"({t.type}{(', ' + ', '.join(extra)) if extra else ''})"
        alias = f" [Aliases:  {', '.join(t.aliases)}]" if t.aliases else ""
        lines.append(f"- {t.source} → {t.target}{tag}{alias}")
    return "\n".join(lines)


def render_annotation_contexts(contexts: list[list[dict[str, str]]]) -> str:
    """Deduplicate annotation data into stable JSON while retaining applicable batch indices."""
    rendered_by_key: dict[str, dict[str, object]] = {}
    for segment_index, items in enumerate(contexts):
        for item in items:
            target_key = item["target_key"]
            source = item["source"]
            rendered = rendered_by_key.get(target_key)
            if rendered is None:
                rendered_by_key[target_key] = {
                    "target_key": target_key,
                    "source": source,
                    "applies_to": [segment_index],
                }
                continue
            if rendered["source"] != source:
                raise ValueError(f"Inconsistent text for annotation target: {target_key}")
            applies_to = rendered["applies_to"]
            if isinstance(applies_to, list) and segment_index not in applies_to:
                applies_to.append(segment_index)
    return json.dumps(list(rendered_by_key.values()), ensure_ascii=False, indent=2)


def numbered(texts: list[str]) -> str:
    """Render text with zero-based indices in square brackets."""
    return "\n".join(f"[{i}] {t}" for i, t in enumerate(texts))


def render_source_reference(source: str) -> str:
    """Quote one following source segment without adding numbered translation inputs."""
    return json.dumps(source, ensure_ascii=False) if source.strip() else "(none)"


def numbered_pairs(sources: list[str], targets: list[str]) -> str:
    """Render aligned source/target pairs for review prompts."""
    out = []
    for i, (s, t) in enumerate(zip(sources, targets)):
        out.append(f"[{i}] Source: {s}\n    Translation: {t}")
    return "\n".join(out)


def numbered_pairs_with_refs(
    sources: list[str],
    targets: list[str],
    refs: list[str],
) -> str:
    """Render source/target pairs with stable segment references for evidence review."""
    out = []
    for index, (source, target) in enumerate(zip(sources, targets)):
        ref = refs[index] if index < len(refs) else ""
        out.append(f"[{index}] ref={ref or '(none)'} Source: {source}\n    Translation: {target}")
    return "\n".join(out)
