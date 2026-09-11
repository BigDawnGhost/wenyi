"""识别书内占位标题，不负责 EPUB 包解析。"""

from __future__ import annotations

import posixpath


def looks_like_internal_title(title: str, href: str, book_title: str = "") -> bool:
    base = posixpath.basename(href).rsplit(".", 1)[0]
    stripped = title.strip()
    return (bool(base) and stripped == base) or (
        bool(book_title) and stripped == book_title.strip()
    )
