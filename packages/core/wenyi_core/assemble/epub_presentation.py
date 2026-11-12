"""Apply EPUB writing direction, language and bilingual presentation styles."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.exceptions import ParserRejectedMarkup

from wenyi_core.assemble.html_bilingual import (
    _BILINGUAL_CSS,
    _BILINGUAL_STYLE_ID,
)

_HTML_EXTS = (".xhtml", ".html", ".htm")


_VERTICAL_MARKERS = (
    re.compile(
        rb"(?:-epub-|-webkit-)?writing-mode\s*:\s*(?:vertical-rl|vertical-lr|tb-rl)",
        re.IGNORECASE,
    ),
    re.compile(
        rb"page-progression-direction\s*=\s*['\"]rtl['\"]",
        re.IGNORECASE,
    ),
    re.compile(
        rb"\bclass\s*=\s*['\"][^'\"]*\bvrtl\b",
        re.IGNORECASE,
    ),
)


_HORIZONTAL_OVERRIDE_ID = "trans-novel-horizontal-override"


_XML_ENCODING = re.compile(
    r"(<\?xml[^>]*\bencoding\s*=\s*)(['\"])[^'\"]+\2",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EpubPresentation:
    """Book-wide decisions passed explicitly to independent XHTML render tasks."""

    lang: str
    force_horizontal: bool


def _epub_looks_vertical(zf: zipfile.ZipFile) -> bool:
    """Detect whether an EPUB declares vertical layout."""
    for info in zf.infolist():
        low = info.filename.lower()
        if not low.endswith((".opf", ".css", ".xhtml", ".html", ".htm")):
            continue
        try:
            data = zf.read(info.filename)
        except (
            EOFError,
            KeyError,
            NotImplementedError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
        ):
            data = b""
        if any(marker.search(data) for marker in _VERTICAL_MARKERS):
            return True
    return False


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
        soup = BeautifulSoup(text, "html.parser")
        html = soup.find("html")
        if html is None:
            return text.encode("utf-8")
        html["lang"] = lang
        html["xml:lang"] = lang
        classes = html.get("class")
        if isinstance(classes, list) and "vrtl" in classes:
            html["class"] = " ".join(str(c) for c in classes if c != "vrtl")

        if force_horizontal and soup.find(id=_HORIZONTAL_OVERRIDE_ID) is None:
            head = soup.find("head")
            if head is None:
                head = soup.new_tag("head")
                html.insert(0, head)
            style = soup.new_tag("style", id=_HORIZONTAL_OVERRIDE_ID)
            style.string = (
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
                "}"
            )
            head.append(style)

        if bilingual and soup.find(id=_BILINGUAL_STYLE_ID) is None:
            head = soup.find("head")
            if head is None:
                head = soup.new_tag("head")
                html.insert(0, head)
            style = soup.new_tag("style", id=_BILINGUAL_STYLE_ID)
            style.string = _BILINGUAL_CSS
            head.append(style)
        output = _XML_ENCODING.sub(r'\1"utf-8"', str(soup))
        return output.encode("utf-8")
    except (ParserRejectedMarkup, UnicodeError, ValueError):
        return data if isinstance(data, bytes) else data.encode("utf-8")
