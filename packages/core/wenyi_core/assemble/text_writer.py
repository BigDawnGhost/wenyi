"""Plain-text and Markdown output.
Read RunStore chapters, merge continuations with _merged_paragraphs and produce monolingual
or bilingual text according to bilingual and order.
"""

from __future__ import annotations

from ..atomicio import publish_target
from ..markup import _epub_native
from .export_view import AssembleStore
from .writer_common import _merged_paragraphs


def _assemble_plain_text(
    store: AssembleStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    markdown: bool = False,
) -> str:
    """Shared text/Markdown implementation; prefix headings with # when markdown=True."""
    m = store.load_manifest()
    chapters = [_merged_paragraphs(store.load_chapter(row["index"])) for row in m["chapters"]]
    with publish_target(out_path) as temporary:
        _epub_native.write_text(temporary, chapters, bilingual, order == "source_first", markdown)
    return out_path


# Plain text.
def _assemble_text(
    store: AssembleStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """Rebuild UTF-8 text by chapter and paragraph, optionally including bilingual source text."""
    return _assemble_plain_text(store, out_path, bilingual=bilingual, order=order)


# ── markdown ──────────────────────────────────────────────────────────────────
def _assemble_markdown(
    store: AssembleStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """Rebuild Markdown with # headings and optional bilingual source text."""
    return _assemble_plain_text(store, out_path, bilingual=bilingual, order=order, markdown=True)
