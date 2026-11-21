"""Pass rendered chapters and already-resolved assets to the native EPUB 3 writer."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from wenyi_core.assemble.about import AboutPage
from wenyi_core.assemble.html_bilingual import _BILINGUAL_CSS
from wenyi_core.atomicio import publish_target
from wenyi_core.markup import _epub_native


@dataclass(frozen=True)
class EpubChapter:
    """A rendered chapter with an OPF-relative href."""

    href: str
    title: str
    content: str


@dataclass(frozen=True)
class EpubResource:
    """An already-resolved resource; the writer performs no external lookup."""

    href: str
    media_type: str
    content: bytes
    cover_image: bool = False


@dataclass
class EpubTocEntry:
    """One shared NAV/NCX entry."""

    href: str
    title: str
    children: list[EpubTocEntry] = field(default_factory=list)


def write_epub(
    out_path: str | Path,
    *,
    title: str,
    lang: str,
    chapters: list[EpubChapter],
    toc: list[EpubTocEntry],
    resources: list[EpubResource],
    about: AboutPage | None = None,
    styled_bilingual: bool = False,
    split_nested_toc: bool = False,
) -> None:
    """Publish a finished EPUB 3 atomically."""
    state = json.dumps(
        {
            "title": title,
            "lang": lang,
            "modified": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "chapters": [asdict(chapter) for chapter in chapters],
            "toc": [asdict(entry) for entry in toc],
            "about": about.render().decode("utf-8") if about is not None else None,
            "bilingual_css": _BILINGUAL_CSS if styled_bilingual else None,
            "split_nested_toc": split_nested_toc,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assets = [(r.href, r.media_type, r.content, r.cover_image) for r in resources]
    with publish_target(out_path) as temporary:
        _epub_native.generate(temporary, state, assets)
