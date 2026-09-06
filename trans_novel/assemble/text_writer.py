"""Plain-text and Markdown output.
Read RunStore chapters, merge continuations with _merged_paragraphs and produce monolingual
or bilingual text according to bilingual and order.
"""

from __future__ import annotations

from ..ingest.models import KIND_HEADING
from ..pipeline.runstore import RunStore
from .writer_common import _bilingual_source, _merged_paragraphs, _ordered_pair


def _assemble_plain_text(
    store: RunStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    markdown: bool = False,
) -> str:
    """Shared text/Markdown implementation; prefix headings with # when markdown=True."""
    m = store.load_manifest()
    chapter_blocks: list[str] = []
    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        if markdown:
            level = ch.meta.get("heading_level", 1)
            level = level if isinstance(level, int) and 1 <= level <= 6 else 1
            heading_prefix = "#" * level + " "
        else:
            heading_prefix = ""
        blocks: list[str] = []
        for kind, target, source in _merged_paragraphs(ch):
            if kind == KIND_HEADING and heading_prefix:
                target = heading_prefix + target
            src = _bilingual_source(source, target) if (bilingual and kind != KIND_HEADING) else ""
            if not src:
                blocks.append(target)
            else:
                first, second = _ordered_pair(src, target, order)
                blocks.extend((first, second))
        chapter_blocks.append("\n\n".join(blocks))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(chapter_blocks) + "\n")
    return out_path


# Plain text.
def _assemble_text(
    store: RunStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """Rebuild UTF-8 text by chapter and paragraph, optionally including bilingual source text."""
    return _assemble_plain_text(store, out_path, bilingual=bilingual, order=order)


# ── markdown ──────────────────────────────────────────────────────────────────
def _assemble_markdown(
    store: RunStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """Rebuild Markdown with # headings and optional bilingual source text."""
    return _assemble_plain_text(store, out_path, bilingual=bilingual, order=order, markdown=True)
