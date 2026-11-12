"""Backfill a complete HTML source, keeping chapter state independent of layout."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypedDict

from bs4 import BeautifulSoup

from wenyi_core.ingest.html_reader import _DEFAULT_CHAPTER_TAGS, annotate_html_document
from wenyi_core.markup.contracts import HTML_ATTRIBUTE_META_KEY, HTML_MARKUP_META_KEY
from wenyi_core.models.document import Chapter

from .html_renderer import _render_segments_html, _segment_render_maps


class _HtmlParseOptions(TypedDict):
    encoding: str
    preserve_inline_markup: bool
    chapter_tags: frozenset[str] | None


def _html_parse_options(meta: dict, chapters: list[Chapter]) -> _HtmlParseOptions:
    """Use the recorded extraction policy, or recognize pre-policy HTML chapter state."""
    policy = meta.get("html_parse")
    if policy is None:
        encoding = "utf-8"
        preserve_inline_markup = any(
            HTML_MARKUP_META_KEY in segment.meta or HTML_ATTRIBUTE_META_KEY in segment.meta
            for chapter in chapters
            for segment in chapter.segments
        )
    elif (
        isinstance(policy, dict)
        and policy.get("version") == 1
        and isinstance(policy.get("encoding"), str)
        and isinstance(policy.get("preserve_inline_markup"), bool)
    ):
        encoding = policy["encoding"]
        preserve_inline_markup = policy["preserve_inline_markup"]
    else:
        raise ValueError("HTML state has an unsupported parsing policy")
    chapter_tags = meta.get("chapter_tags", sorted(_DEFAULT_CHAPTER_TAGS))
    if chapter_tags is not None and not (
        isinstance(chapter_tags, list) and all(isinstance(tag, str) for tag in chapter_tags)
    ):
        raise ValueError("HTML state has invalid chapter boundary tags")
    return {
        "encoding": encoding,
        "preserve_inline_markup": preserve_inline_markup,
        "chapter_tags": frozenset(chapter_tags) if chapter_tags is not None else None,
    }


def _render_html_source(
    source_path: str,
    manifest: dict,
    chapters: list[Chapter],
    *,
    bilingual: bool,
    order: str,
    preserve_source_style: bool,
    preserve_source_ruby: bool | None,
) -> str:
    """Regenerate deterministic anchors, validate saved sources, then apply translations.

    The original file owns layout. Saved chapters own translations and alignment results,
    not the head, shell or chapter templates. No state is changed or model called here.
    """
    raw_meta = manifest.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    options = _html_parse_options(meta, chapters)
    data = Path(source_path).read_bytes()
    source_hash = manifest.get("source_sha256")
    if source_hash and source_hash != hashlib.sha256(data).hexdigest():
        raise ValueError("HTML source does not match translation state: source file changed")
    try:
        html = data.decode(options["encoding"], errors="replace")
    except LookupError as exc:
        raise ValueError("HTML state has an unsupported source encoding") from exc
    if meta.get("html_parse") is None and not options["preserve_inline_markup"]:
        original = BeautifulSoup(html, "html.parser")
        if original.html is not None and original.body is None:
            # The original parser treated the entire html node as one fragment.
            # Inferring body must not change paid state's chapter/anchor identities.
            options["chapter_tags"] = None
    document, template = annotate_html_document(
        html,
        source_path,
        manifest.get("source_lang", ""),
        manifest.get("target_lang", ""),
        **options,
    )
    fresh = {
        segment.anchor: segment
        for chapter in document.chapters
        for segment in chapter.segments
        if segment.anchor
    }
    stored_sources: dict[str, str] = {}
    for chapter in chapters:
        if chapter.segments and chapter.segments[0].cont:
            raise ValueError("HTML state is inconsistent: chapter starts with a continuation")
        seen: set[str] = set()
        for segment in chapter.segments:
            if segment.anchor and not segment.cont:
                if segment.anchor in seen or segment.anchor in stored_sources:
                    raise ValueError(f"HTML state has duplicate backfill anchors: {segment.anchor}")
                seen.add(segment.anchor)
        _targets, sources, _kinds, _metadata = _segment_render_maps(chapter.segments)
        stored_sources.update(sources)
    missing = sorted(set(stored_sources) - set(fresh))
    if missing:
        raise ValueError(
            "HTML source does not match translation state: lacks backfill anchors "
            + ", ".join(missing[:3])
        )
    changed = [
        anchor for anchor, source in stored_sources.items() if fresh[anchor].source != source
    ]
    if changed or set(fresh) != set(stored_sources):
        raise ValueError("HTML source does not match translation state: extracted content changed")
    return _render_segments_html(
        template,
        [segment for chapter in chapters for segment in chapter.segments],
        render_meta_by_anchor={anchor: segment.meta for anchor, segment in fresh.items()},
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
        source_lang=manifest.get("source_lang", ""),
        preserve_source_ruby=preserve_source_ruby,
    )
