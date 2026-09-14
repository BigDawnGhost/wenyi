"""Ruby reading markers shared by document ingestion, matching and rendering."""

from __future__ import annotations

import re

RUBY_MARK_LEFT = "〘"


RUBY_MARK_RIGHT = "〙"


_RUBY_MARK_RE = re.compile(r"〘[^〙]*〙")


def strip_ruby_markers(text: str) -> str:
    """Strip pronunciation hint markers for glossary matching and accidental-copy recovery."""
    if not text or RUBY_MARK_LEFT not in text:
        return text
    return _RUBY_MARK_RE.sub("", text)
