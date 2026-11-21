"""Apply EPUB writing direction, language and bilingual presentation styles."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.exceptions import ParserRejectedMarkup

from wenyi_core.assemble.html_bilingual import (
    _BILINGUAL_CSS,
    _BILINGUAL_STYLE_ID,
)
from wenyi_core.markup.epub_dom import parse_epub_dom

_HORIZONTAL_OVERRIDE_ID = "trans-novel-horizontal-override"


_XML_ENCODING = re.compile(
    r"(<\?xml[^>]*\bencoding\s*=\s*)(['\"])[^'\"]+\2",
    re.IGNORECASE,
)


def _rewrite_html_document(
    data: bytes | str,
    *,
    lang: str,
    force_horizontal: bool,
    bilingual: bool = False,
) -> bytes:
    """Set the target HTML language and inject horizontal-layout or bilingual styles as needed."""
    try:
        if isinstance(data, bytes):
            text = UnicodeDammit(data).unicode_markup
            if text is None:
                text = data.decode("utf-8", errors="replace")
        else:
            text = data
        soup = parse_epub_dom(text)
        if not _apply_html_presentation(
            soup, lang=lang, force_horizontal=force_horizontal, bilingual=bilingual
        ):
            return text.encode("utf-8")
        return _serialize_presented_dom(soup).encode("utf-8")
    except (ParserRejectedMarkup, UnicodeError, ValueError):
        return data if isinstance(data, bytes) else data.encode("utf-8")


def _serialize_presented_dom(soup: BeautifulSoup) -> str:
    """Align the declaration with the UTF-8 bytes the EPUB writer publishes."""
    return _XML_ENCODING.sub(r'\1"utf-8"', str(soup))


def _apply_html_presentation(
    soup: BeautifulSoup,
    *,
    lang: str,
    force_horizontal: bool,
    bilingual: bool,
) -> bool:
    """Mutate the same DOM used for backfill rather than reparsing its output."""
    html = soup.find("html")
    if html is None:
        return False
    html["lang"] = lang
    html["xml:lang"] = lang
    classes = html.get("class")
    if isinstance(classes, list) and "vrtl" in classes:
        html["class"] = " ".join(str(c) for c in classes if c != "vrtl")

    def add_style(style_id: str, css: str) -> None:
        if soup.find(id=style_id) is not None:
            return
        head = soup.find("head")
        if head is None:
            head = soup.new_tag("head")
            html.insert(0, head)
        style = soup.new_tag("style", id=style_id)
        style.string = css
        head.append(style)

    if force_horizontal:
        add_style(
            _HORIZONTAL_OVERRIDE_ID,
            "html, body { "
            "writing-mode: horizontal-tb !important; "
            "-epub-writing-mode: horizontal-tb !important; "
            "-webkit-writing-mode: horizontal-tb !important; "
            "direction: ltr !important; "
            "text-orientation: mixed !important; "
            "} "
            '.vrtl, .vertical, [class*="vrtl"] { '
            "writing-mode: horizontal-tb !important; "
            "-epub-writing-mode: horizontal-tb !important; "
            "-webkit-writing-mode: horizontal-tb !important; "
            "direction: ltr !important; "
            "}",
        )
    if bilingual:
        add_style(_BILINGUAL_STYLE_ID, _BILINGUAL_CSS)
    return True
