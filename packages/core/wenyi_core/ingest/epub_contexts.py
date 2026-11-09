"""Resolve annotation contexts independently per target resource, then merge in source order."""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup

from wenyi_core.markup.anchors import fragment_nodes, resolve_epub_href, scope_segment_anchors
from wenyi_core.markup.annotations import _note_context_scope
from wenyi_core.markup.contracts import ANNOTATION_META_KEY
from wenyi_core.models.document import Segment
from wenyi_core.resource_pool import ResourcePool


@dataclass(frozen=True)
class _Reference:
    ordinal: int
    resource_href: str
    segment_anchor: str | None
    fragment: str
    target_key: str
    relation: str | None


@dataclass(frozen=True)
class _ContextTask:
    href: str
    template: str
    sources: dict[str, str]
    references: list[_Reference]


@dataclass(frozen=True)
class _ResolvedContext:
    ordinal: int
    promote_to_noteref: bool
    context: dict[str, object] | None


def _resolve_resource_contexts(task: _ContextTask) -> list[_ResolvedContext]:
    """Parse one target without mutating any referring Segment or annotation."""
    soup = BeautifulSoup(task.template, "html.parser")
    results: list[_ResolvedContext] = []
    for reference in task.references:
        nodes = fragment_nodes(soup, reference.fragment)
        if len(nodes) != 1:
            # Duplicate id/name values make reference ownership ambiguous.
            continue
        scope, semantic_note = _note_context_scope(nodes[0], reference.fragment)
        promote = reference.relation == "internal_link" and semantic_note
        relation = "noteref" if promote else reference.relation
        context: dict[str, object] | None = None
        if relation == "noteref":
            anchors = [anchor for anchor in scope_segment_anchors(scope) if anchor in task.sources]
            self_link = (
                task.href == reference.resource_href
                and reference.segment_anchor is not None
                and anchors == [reference.segment_anchor]
            )
            blocks = [task.sources[anchor] for anchor in anchors if task.sources[anchor].strip()]
            if blocks and not self_link:
                context = {
                    "target_key": reference.target_key,
                    "resource_href": task.href,
                    "fragment": reference.fragment,
                    "source_blocks": blocks,
                    "segment_anchors": anchors,
                }
        results.append(_ResolvedContext(reference.ordinal, promote, context))
    return results


def _build_epub_annotation_contexts(
    reference_resources: list[dict[str, object]],
    lookup_resources: list[dict[str, object]],
    *,
    pool: ResourcePool | None = None,
) -> dict[str, object]:
    """Group by target XHTML; publish relation changes and contexts in reference order."""
    lookup = {
        resource["href"]: resource
        for resource in lookup_resources
        if isinstance(resource.get("href"), str) and resource["href"]
    }
    references: dict[str, list[_Reference]] = {}
    items: list[dict] = []
    for resource in reference_resources:
        raw_segments = resource.get("segments")
        for segment in raw_segments if isinstance(raw_segments, list) else []:
            if not isinstance(segment, Segment):
                continue
            raw_annotations = segment.meta.get(ANNOTATION_META_KEY)
            annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
            raw_items = annotations.get("items")
            for item in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(item, dict) or not isinstance(item.get("raw_href"), str):
                    continue
                resolved = resolve_epub_href(segment.resource_href or "", item["raw_href"])
                if (
                    resolved.external
                    or not resolved.resource_href
                    or not resolved.fragment
                    or resolved.target_key != item.get("target_key")
                    or resolved.resource_href not in lookup
                ):
                    continue
                relation = item.get("relation")
                references.setdefault(resolved.resource_href, []).append(
                    _Reference(
                        len(items),
                        segment.resource_href or "",
                        segment.anchor,
                        resolved.fragment,
                        resolved.target_key,
                        relation if isinstance(relation, str) else None,
                    )
                )
                items.append(item)

    tasks: list[_ContextTask] = []
    for href, refs in references.items():
        resource = lookup[href]
        template = resource.get("template")
        if not isinstance(template, str):
            continue
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        sources = {
            segment.anchor: segment.source
            for segment in segments
            if isinstance(segment, Segment) and isinstance(segment.anchor, str) and segment.anchor
        }
        tasks.append(_ContextTask(href, template, sources, refs))

    batches = (
        pool.map(_resolve_resource_contexts, tasks)
        if pool is not None
        else map(_resolve_resource_contexts, tasks)
    )
    results = sorted(
        (result for batch in batches for result in batch), key=lambda result: result.ordinal
    )
    contexts: dict[str, dict[str, object]] = {}
    for result in results:
        if result.promote_to_noteref:
            items[result.ordinal]["relation"] = "noteref"
        if result.context is not None:
            contexts.setdefault(str(result.context["target_key"]), result.context)
    return {"version": 1, "contexts": contexts}
