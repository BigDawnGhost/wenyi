"""Canonical EPUB rendering API."""

from trans_novel.assemble.epub.rendering.bilingual import (
    BILINGUAL_DIRECT_TARGET_ATTRS,
    BILINGUAL_DIRECT_TARGET_CLASS,
    BILINGUAL_SOURCE_CLASS,
    dedupe_segment_mappings,
    direct_run_add_whitespace,
    direct_run_boundary,
    direct_run_has_active_ancestor,
    direct_run_is_active,
    direct_run_source_copy,
    is_bilingual_container_tag,
    japanese_ruby_source_copy,
    ruby_base_count,
    sanitized_source_copy,
    segment_needs_source,
    source_node_is_valid,
)
from trans_novel.assemble.epub.rendering.generated import build_epub_from_chapters
from trans_novel.assemble.epub.rendering.source_archive import assemble_epub, assemble_source_epub
from trans_novel.assemble.epub.rendering.source_dom import bilingual_source_copy
from trans_novel.assemble.epub.rendering.source_markup import (
    add_bilingual_sources,
    rewrite_toc_lxml,
)

__all__ = [
    "BILINGUAL_DIRECT_TARGET_ATTRS",
    "BILINGUAL_DIRECT_TARGET_CLASS",
    "BILINGUAL_SOURCE_CLASS",
    "add_bilingual_sources",
    "assemble_epub",
    "assemble_source_epub",
    "bilingual_source_copy",
    "build_epub_from_chapters",
    "dedupe_segment_mappings",
    "direct_run_add_whitespace",
    "direct_run_boundary",
    "direct_run_has_active_ancestor",
    "direct_run_is_active",
    "direct_run_source_copy",
    "is_bilingual_container_tag",
    "japanese_ruby_source_copy",
    "rewrite_toc_lxml",
    "ruby_base_count",
    "sanitized_source_copy",
    "segment_needs_source",
    "source_node_is_valid",
]
