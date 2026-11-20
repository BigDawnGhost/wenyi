"""Build EPUB DOMs from native XHTML tokens, with one tolerant HTML fallback."""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import CData, Comment, Doctype, ProcessingInstruction

try:
    from wenyi_core.markup._epub_native import xhtml_tokens
except ModuleNotFoundError as exc:
    # A checkout can be inspected without an editable build. An installed extension
    # that fails to load is a packaging error, not an input compatibility fallback.
    if exc.name != "wenyi_core.markup._epub_native":
        raise
    xhtml_tokens = None


def parse_epub_dom(markup: str) -> BeautifulSoup:
    """Use the same tree builder and serialization rules for both tokenizers."""
    tokens = xhtml_tokens(markup) if xhtml_tokens is not None else None
    if tokens is None:
        return BeautifulSoup(markup, "html.parser", store_line_numbers=False)

    soup = BeautifulSoup("", "html.parser", store_line_numbers=False)
    for kind, value, attrs in tokens:
        if kind == 0:
            soup.handle_data(value)
        elif kind in (1, 2):
            soup.handle_starttag(value, None, None, dict(attrs))
            if kind == 2:
                soup.handle_endtag(value)
        elif kind == 3:
            soup.handle_endtag(value)
        else:
            soup.endData()
            soup.handle_data(value)
            soup.endData((Comment, Doctype, ProcessingInstruction, CData)[kind - 4])
    soup.endData()
    return soup
