"""Standalone HTML output.
Read the manifest and chapters to find reusable HTML/PDF templates. Backfill template nodes
with html_renderer and materialize assets with html_resources; otherwise reconstruct
headings, paragraphs and bilingual content.
"""

from __future__ import annotations

import os
import posixpath
import re
import zipfile
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup
from bs4.element import Tag

from wenyi_core.assemble.epub_resources import _epub_resource_specs, _render_epub_resources
from wenyi_core.assemble.html_bilingual import _BILINGUAL_CSS, _BILINGUAL_STYLE_ID
from wenyi_core.assemble.html_renderer import _render_chapter_html
from wenyi_core.atomicio import publish_target

from ..ingest.epub_package import _decode_markup
from ..models.document import KIND_HEADING
from .export_view import AssembleStore
from .html_resources import (
    _MEDIA_SUFFIXES,
    _materialize_html_resources,
    _package_html_resources,
    _template_resource_source,
    _write_packaged,
)
from .policy import export_options
from .writer_common import _bilingual_source, _epub_lang, _manifest_target_lang, _merged_paragraphs


def _assemble_html(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Backfill chapter templates and combine them into a complete HTML document."""
    if os.path.abspath(source_path) == os.path.abspath(out_path):
        raise ValueError("--out must not point at the source file; choose a different output path")
    m = store.load_manifest()
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_head_html = meta.get("head_html", "")
    head_html = raw_head_html if isinstance(raw_head_html, str) else ""
    # Always declare the charset so browsers decode Unicode text correctly. Source heads
    # can carry stale declarations (e.g. gb2312 for content that is now UTF-8), so every
    # meta charset is stripped before adding our own (audit X-4).
    head_html = re.sub(r"<meta[^>]*charset[^>]*>", "", head_html, flags=re.IGNORECASE)
    head_html = '<meta charset="utf-8"/>\n' + head_html
    if bilingual and not preserve_source_style and _BILINGUAL_STYLE_ID not in head_html:
        head_html += f'<style id="{_BILINGUAL_STYLE_ID}">\n{_BILINGUAL_CSS}</style>'

    body_parts: list[str] = []
    rendered_epub = False
    packaged: dict[str, tuple[str, bytes]] = {}
    if m.get("fmt") == "epub" and _epub_resource_specs(meta):
        chapters = [store.load_chapter(c["index"]) for c in m["chapters"]]
        asset_prefix = f"{Path(out_path).stem}.assets"
        with zipfile.ZipFile(source_path, "r") as archive:
            # Media entries are read once here: their bytes end up in the .assets
            # directory either way, and keeping the XHTML out of memory bounds usage.
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
                preserve_source_ruby=export_options(store, "html").preserve_source_ruby,
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
                # EPUB references are page-relative; resolve them against the page's own
                # zip directory and rewrite into the flat .assets namespace (audit X-1).
                rewritten, page_packaged = _package_html_resources(
                    body_html,
                    source_dir=posixpath.dirname(href),
                    href_prefix=asset_prefix,
                    zip_resources=media,
                )
                packaged.update(page_packaged)
                body_parts.append(rewritten)
        rendered_epub = bool(body_parts)

    for c in [] if rendered_epub else m["chapters"]:
        ch = store.load_chapter(c["index"])
        if ch.template:
            # Reuse EPUB rendering for data-tn-id replacement, continuations and bilingual content.
            body_parts.append(
                _render_chapter_html(
                    ch,
                    bilingual=bilingual,
                    order=order,
                    preserve_source_style=preserve_source_style,
                    preserve_source_ruby=export_options(store, "html").preserve_source_ruby,
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

    full_html = f"""<!DOCTYPE html>
<html lang="{escape(_epub_lang(_manifest_target_lang(m)))}">
<head>
{head_html}
</head>
<body>
{"".join(body_parts)}
</body>
</html>"""
    if m.get("fmt") == "epub":
        # EPUB references were resolved only inside the ZIP. An empty result must not
        # retry unresolved references against files beside the source EPUB.
        _write_packaged(packaged, out_path)
    else:
        full_html = _materialize_html_resources(
            full_html,
            source_path=_template_resource_source(store, m, source_path),
            out_path=out_path,
        )

    # Publish atomically: the .assets directory is keyed to the final out_path, only the
    # document file itself is swapped (audit X-2).
    with publish_target(out_path) as temp_out:
        with open(temp_out, "w", encoding="utf-8") as f:
            f.write(full_html)
    return out_path
