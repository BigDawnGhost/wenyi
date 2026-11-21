"""Public entry point for translation assembly.
Format implementations live in writer_common (shared helpers), text_writer (TXT/Markdown),
html_renderer (DOM), html_resources (assets), html_writer, pdf_writer, docx_writer and
epub_writer.
"""

from __future__ import annotations

from typing import Literal

from ..i18n.policy.models import PolicyContext, PolicyPlan
from ..i18n.policy.resolver import resolve_policy
from .about import AboutPage
from .docx_writer import _assemble_docx
from .epub_writer import (
    _assemble_epub,
    _build_epub_from_chapters,
    _build_epub_from_html_templates,
)
from .export_view import AssembleStore, ExportViewStore
from .html_writer import _assemble_html
from .pdf_writer import _assemble_pdf
from .text_writer import _assemble_markdown, _assemble_text
from .writer_common import (
    _OUT_EXT,
    _default_out,
    _ensure_parent_dir,
    _manifest_target_lang,
    default_output_format,
    output_backend,
    supports_pdf_export,
)

__all__ = ["assemble"]


def assemble(
    store: AssembleStore,
    source_path: str,
    out_path: str | None = None,
    out_format: str | None = None,
    *,
    bilingual: bool = False,
    order: Literal["target_first", "source_first"] = "target_first",
    preserve_source_style: bool = False,
    about_page: bool = True,
    babeldoc_timeout: float = 600.0,
    language_policy: PolicyPlan | None = None,
) -> str:
    """Generate translated output, defaulting to PDF for BabelDOC state and EPUB otherwise.
    EPUB input reuses the original layout and resources; template-free input produces a
    standard EPUB with headings and paragraphs. TXT and Markdown rebuild chapters. HTML
    prefers source templates and otherwise rebuilds chapters. PDF is only available through
    BabelDOC layout-preserving fillback. DOCX reconstructs headings, paragraphs and tables.
    With bilingual=True, include source text in the requested order. PDF pairs matching pages
    left to right in that order. preserve_source_style reuses
    original styles instead of muted CSS in EPUB/HTML or plain source text in DOCX.
    about_page appends the translation about page.
    """
    if out_format is not None and out_format not in _OUT_EXT:
        supported = " / ".join(_OUT_EXT)
        raise ValueError(f"Unsupported output format: {out_format} (supported: {supported})")

    m = store.load_manifest()
    if out_format is None:
        out_format = default_output_format(m)
    backend = output_backend(m, out_format)
    if language_policy is None:
        language_policy = resolve_policy(
            PolicyContext(
                m.get("source_lang", "auto"),
                _manifest_target_lang(m),
                phase="export",
                format=out_format,
                backend=backend,
                source_identity=m.get("source_sha256", ""),
                bilingual=bilingual,
                order=order,
                preserve_source_style=preserve_source_style,
                about_page=about_page,
            )
        )
    if (
        language_policy.context.phase != "export"
        or language_policy.context.format != out_format
        or language_policy.context.backend != backend
    ):
        raise ValueError("Export language policy does not match the actual output format")
    view = (
        store
        if isinstance(store, ExportViewStore)
        else ExportViewStore(store, plan=language_policy)
    )
    m = view.load_manifest()
    # EPUB and PDF writers project all chapters before opening output or making
    # a bridge request. Prewarming their view would deep-copy the same chapters twice.
    if out_format not in {"epub", "pdf"}:
        view.prepare()
    target_lang = _manifest_target_lang(m)
    if out_format == "txt":
        out_path = out_path or _default_out(
            source_path, "txt", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_text(view, out_path, bilingual=bilingual, order=order)
    if out_format == "html":
        out_path = out_path or _default_out(
            source_path, "html", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_html(
            view,
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
        return _assemble_markdown(view, out_path, bilingual=bilingual, order=order)
    if out_format == "pdf":
        out_path = out_path or _default_out(
            source_path, "pdf", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_pdf(
            view, source_path, out_path, timeout=babeldoc_timeout, bilingual=bilingual, order=order
        )
    if out_format == "docx":
        out_path = out_path or _default_out(
            source_path, "docx", "", bilingual=bilingual, target_lang=target_lang
        )
        _ensure_parent_dir(out_path)
        return _assemble_docx(
            view,
            out_path,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
    out_path = out_path or _default_out(
        source_path, "epub", "", bilingual=bilingual, target_lang=target_lang
    )
    _ensure_parent_dir(out_path)
    about = (
        AboutPage(language_policy.export.language_tag, language_policy.export.about_locale)
        if about_page
        else None
    )
    if m["fmt"] == "epub":
        writer = _assemble_epub
    elif m["fmt"] == "html" or (m["fmt"] == "pdf" and not supports_pdf_export(m)):
        writer = _build_epub_from_html_templates
    else:
        # BabelDOC snapshots have paragraph IDs, not MinerU HTML templates or anchors.
        writer = _build_epub_from_chapters
    return writer(
        view,
        source_path,
        out_path,
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
        about=about,
    )
