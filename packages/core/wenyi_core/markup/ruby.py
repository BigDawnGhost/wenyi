"""Ruby reading markers shared by document ingestion, matching and rendering."""

from __future__ import annotations

import re

RUBY_MARK_LEFT = "〘"


RUBY_MARK_RIGHT = "〙"


_RUBY_MARK_RE = re.compile(r"〘(?!object:)[^〙]*〙")


def strip_ruby_markers(text: str) -> str:
    """Strip pronunciation hints, reserving object markers for validated object restoration."""
    if not text or RUBY_MARK_LEFT not in text:
        return text
    return _RUBY_MARK_RE.sub("", text)
