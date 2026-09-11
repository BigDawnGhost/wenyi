"""Public entry point for translation assembly.
Format implementations live in writer_common (shared helpers), text_writer (TXT/Markdown),
html_renderer (DOM), html_resources (assets), html_writer, pdf_writer, docx_writer and
epub_writer.
"""

from __future__ import annotations

from ..pipeline.runstore import RunStore
from .about import append_about_page
from .docx_writer import _assemble_docx
from .epub_writer import (
    _assemble_epub,
    _build_epub_from_chapters,
    _build_epub_from_html_templates,
)
from .export_view import ExportViewStore
from .html_writer import _assemble_html
from .pdf_writer import _assemble_pdf
from .text_writer import _assemble_markdown, _assemble_text
from .writer_common import (
    _OUT_EXT,
    _default_out,
    _ensure_parent_dir,
    _epub_lang,
    _manifest_target_lang,
    default_output_format,
)

__all__ = ["assemble"]


def assemble(
    store: RunStore,
    source_path: str,
    out_path: str | None = None,
    out_format: str | None = None,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    about_page: bool = True,
    pdf_engine: str = "weasyprint",
    babeldoc_timeout: float = 600.0,
    punctuation_normalize: bool = False,
) -> str:
    """Generate translated output, defaulting to PDF for BabelDOC state and EPUB otherwise.
    EPUB input reuses the original layout and resources; template-free input produces a
    standard EPUB with headings and paragraphs. TXT and Markdown rebuild chapters. HTML
    prefers source templates and otherwise rebuilds chapters. PDF renders print HTML with
    the selected engine. DOCX reconstructs heading navigation, paragraphs and basic tables.
    With bilingual=True, include source text in the requested order. preserve_source_style
    reuses original styles instead of muted CSS. about_page appends the translation about
    page. punctuation_normalize changes only export copies, never chapter target state.
    """
    if out_format is not None and out_format not in _OUT_EXT:
        supported = " / ".join(_OUT_EXT)
        raise ValueError(f"Unsupported output format: {out_format} (supported: {supported})")

    store = ExportViewStore(store, punctuation_normalize=punctuation_normalize)
    m = store.load_manifest()
    if out_format is None:
        out_format = default_output_format(m)
    target_lang = _manifest_target_lang(m)
    if out_format == "txt":
        out_path = out_path or _default_out(
            source_path, "txt", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_text(store, out_path, bilingual=bilingual, order=order)
    if out_format == "html":
        out_path = out_path or _default_out(
            source_path, "html", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_html(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if out_format == "markdown":
        out_path = out_path or _default_out(
            source_path, "markdown", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_markdown(store, out_path, bilingual=bilingual, order=order)
    if out_format == "pdf":
        out_path = out_path or _default_out(
            source_path, "pdf", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_pdf(
            store,
            source_path,
            out_path,
            engine=pdf_engine,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
            babeldoc_timeout=babeldoc_timeout,
        )
    if out_format == "docx":
        out_path = out_path or _default_out(
            source_path, "docx", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_docx(store, out_path, bilingual=bilingual, order=order)
    out_path = out_path or _default_out(
        source_path, "epub", "", bilingual=bilingual, target_lang=target_lang
    )
    _ensure_parent_dir(out_path)
    if m["fmt"] == "epub":
        result = _assemble_epub(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    elif m["fmt"] in {"html", "pdf"}:
        result = _build_epub_from_html_templates(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    else:
        # FB2/text: build a standard EPUB from chapter data.
        result = _build_epub_from_chapters(
            store,
            source_path,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    if about_page:
        append_about_page(result, _epub_lang(target_lang))
    return result
