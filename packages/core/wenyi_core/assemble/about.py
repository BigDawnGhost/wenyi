"""Generate the About This Translation page at the end of a book."""

from __future__ import annotations

from string import Template

from ..atomicio import publish_target
from ..i18n.policy.models import PolicyContext
from ..i18n.policy.resolver import resolve_policy
from ..i18n.resources import read_text
from ..markup import _epub_native

ABOUT_TITLE = "关于此翻译"
ABOUT_FILENAME = "trans-novel-about.xhtml"
ABOUT_REPOSITORY = "https://github.com/BigDawnGhost/wenyi"


def about_xhtml(lang: str, *, locale: str | None = None) -> bytes:
    """Return a standalone XHTML page suitable for the EPUB spine."""
    locale = (
        locale
        or resolve_policy(
            PolicyContext("auto", lang, phase="export", format="epub")
        ).export.about_locale
    )
    return (
        Template(read_text(f"export/about.{locale}.xhtml"))
        # safe_substitute: a literal $ in future template prose must not crash the whole
        # export postprocessing step (audit A-4).
        .safe_substitute(lang=lang, title=ABOUT_TITLE, repository=ABOUT_REPOSITORY)
        .encode("utf-8")
    )


def append_about_page(epub_path: str, lang: str, *, locale: str | None = None) -> bool:
    """Attach the about page without rewriting any unrelated EPUB content."""
    page = about_xhtml(lang, locale=locale).decode("utf-8")
    with publish_target(epub_path) as tmp_path:
        return _epub_native.append_about(epub_path, tmp_path, page)
