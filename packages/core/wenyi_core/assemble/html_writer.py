"""Single-file HTML output with source-backed HTML/EPUB layout and embedded local media."""

from __future__ import annotations

import os
import posixpath
import zipfile
from html import escape

from bs4 import BeautifulSoup
from bs4.element import Tag

from wenyi_core.assemble.epub_resources import _epub_resource_specs, _render_epub_resources
from wenyi_core.assemble.html_bilingual import _BILINGUAL_CSS, _BILINGUAL_STYLE_ID
from wenyi_core.assemble.html_renderer import _html_open_tag, _render_chapter_html
from wenyi_core.atomicio import publish_target
from wenyi_core.markup.html import html_document_soup

from ..ingest.epub_package import _decode_markup
from ..models.document import KIND_HEADING
from .export_view import AssembleStore
from .html_resources import (
    _MEDIA_SUFFIXES,
    _embed_html_resources,
    _template_resource_source,
)
from .html_source import _render_html_source
from .writer_common import _bilingual_source, _epub_lang, _manifest_target_lang, _merged_paragraphs


def _html_presentation(
    html: str, target_lang: str, *, bilingual: bool, preserve_source_style: bool
) -> str:
    """Update output language/encoding without replacing the source head or shell."""
    soup = html_document_soup(html)
    head = soup.head
    assert head is not None and soup.html is not None
    for node in head.find_all("meta"):
        if node.has_attr("charset") or str(node.get("http-equiv", "")).lower() == "content-type":
            node.decompose()
    head.insert(0, soup.new_tag("meta", charset="utf-8"))
    if bilingual and not preserve_source_style and not soup.find(id=_BILINGUAL_STYLE_ID):
        style = soup.new_tag("style", id=_BILINGUAL_STYLE_ID)
        style.string = _BILINGUAL_CSS
        head.append(style)
    soup.html["lang"] = _epub_lang(target_lang)
    if soup.html.has_attr("xml:lang"):
        soup.html["xml:lang"] = _epub_lang(target_lang)
    return str(soup)


def _assemble_html(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Backfill a complete HTML source, or construct HTML for other input formats."""
    if os.path.abspath(source_path) == os.path.abspath(out_path):
        raise ValueError("--out must not point at the source file; choose a different output path")
    m = store.load_manifest()
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_head_html = meta.get("head_html", "")
    head_html = raw_head_html if isinstance(raw_head_html, str) else ""

    body_parts: list[str] = []
    rendered_epub = False
    rendered_source_html: str | None = None
    if m.get("fmt") == "html":
        rendered_source_html = _render_html_source(
            source_path,
            m,
            [store.load_chapter(c["index"]) for c in m["chapters"]],
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    elif m.get("fmt") == "epub" and _epub_resource_specs(meta):
        chapters = [store.load_chapter(c["index"]) for c in m["chapters"]]
        with zipfile.ZipFile(source_path, "r") as archive:
            # Resolve media only inside the EPUB, never against adjacent local files.
            media = {
                info.filename: archive.read(info.filename)
                for info in archive.infolist()
                if os.path.splitext(info.filename)[1].lower() in _MEDIA_SUFFIXES
            }
            rendered = _render_epub_resources(
                archive,
                chapters,
                meta,
                book_title=m.get("title", "") if isinstance(m.get("title"), str) else "",
                bilingual=bilingual,
                order=order,
                preserve_source_style=preserve_source_style,
                source_lang=(
                    m.get("source_lang", "") if isinstance(m.get("source_lang"), str) else ""
                ),
            )
            for _resource_index, href in _epub_resource_specs(meta):
                resource_html = rendered.get(href)
                if not resource_html:
                    # Pages without translated segments (image-only plates) must keep
                    # their original body instead of being silently dropped.
                    try:
                        resource_html = _decode_markup(archive.read(href))
                    except KeyError:
                        continue
                resource_soup = BeautifulSoup(resource_html, "html.parser")
                resource_body = resource_soup.find("body")
                body_html = (
                    resource_body.decode_contents()
                    if isinstance(resource_body, Tag)
                    else str(resource_soup)
                )
                # Embed page-relative media before combining physical resources.
                rewritten = _embed_html_resources(
                    body_html,
                    source_dir=posixpath.dirname(href),
                    zip_resources=media,
                )
                body_parts.append(rewritten)
        rendered_epub = bool(body_parts)

    for c in [] if rendered_epub or rendered_source_html is not None else m["chapters"]:
        ch = store.load_chapter(c["index"])
        if ch.template:
            # Reuse EPUB rendering for data-tn-id replacement, continuations and bilingual content.
            body_parts.append(
                _render_chapter_html(
                    ch,
                    bilingual=bilingual,
                    order=order,
                    preserve_source_style=preserve_source_style,
                )
            )
            continue

        # Template-free inputs such as TXT and Markdown must also export their body text.
        for kind, target, source, level in _merged_paragraphs(ch):
            if kind == KIND_HEADING:
                level = min(level, 6)
                target_html = f"<h{level}>{escape(target)}</h{level}>"
            else:
                target_html = f"<p>{escape(target)}</p>"
            src = _bilingual_source(source, target) if (bilingual and kind != KIND_HEADING) else ""
            if not src:
                body_parts.append(target_html)
                continue
            source_html = f'<p class="tn-source">{escape(src)}</p>'
            if order == "source_first":
                body_parts.extend((source_html, target_html))
            else:
                body_parts.extend((target_html, source_html))

    if rendered_source_html is not None:
        full_html = rendered_source_html
    else:
        html_open = _html_open_tag("html", meta.get("html_attrs"))
        body_open = _html_open_tag("body", meta.get("body_attrs"))
        full_html = f"""<!DOCTYPE html>
{html_open}
<head>
{head_html}
</head>
{body_open}
{"".join(body_parts)}
</body>
</html>"""
    full_html = _html_presentation(
        full_html,
        _manifest_target_lang(m),
        bilingual=bilingual,
        preserve_source_style=preserve_source_style,
    )
    if m.get("fmt") != "epub":
        # EPUB references were already resolved in the container, with no disk fallback.
        full_html = _embed_html_resources(
            full_html,
            source_dir=os.path.dirname(_template_resource_source(store, m, source_path)),
        )

    # The document and all embedded media publish together as one atomic file.
    with publish_target(out_path) as temp_out:
        with open(temp_out, "w", encoding="utf-8") as f:
            f.write(full_html)
    return out_path
