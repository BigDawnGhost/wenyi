"""Public entry point for translation assembly.
Format implementations live in writer_common (shared helpers), text_writer (TXT/Markdown),
html_renderer (DOM), html_resources (assets), html_writer, pdf_writer, docx_writer and
epub_writer.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

from ..i18n.policy.models import PolicyContext, PolicyPlan
from ..i18n.policy.resolver import resolve_policy
from ..ingest.pdf_reader import cached_epub_source, pdf_cache_epub_path
from ..models.document import MINERU_EPUB_DIGEST, uses_epub_template
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
    validate_pdf_source_format,
)

__all__ = ["assemble"]

logger = logging.getLogger(__name__)


def _write_warnings_sidecar(out_path: str, warnings: list[str]) -> None:
    """Write the export warning list beside the artifact so export results stay queryable."""
    sidecar = Path(f"{out_path}.warnings.json")
    sidecar.write_text(json.dumps(warnings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_output_path(
    store: AssembleStore, manifest: dict, source: str, destination: str
) -> None:
    """Protect both the original and the derived template, including staged exports."""
    protected = {os.path.realpath(source)}
    if MINERU_EPUB_DIGEST in (manifest.get("meta") or {}):
        cached = pdf_cache_epub_path(store.source_dir, manifest["source_sha256"])
        protected.add(os.path.realpath(cached))
    if os.path.realpath(destination) in protected:
        raise ValueError("--out must not point at the source file; choose a different output path")


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
    warnings: list[str] | None = None,
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
    warnings collects href resolution failures encountered while building EPUB output; when
    any are recorded they are logged and written to '<out>.warnings.json' beside the artifact.
    """
    if out_format is not None and out_format not in _OUT_EXT:
        supported = " / ".join(_OUT_EXT)
        raise ValueError(f"Unsupported output format: {out_format} (supported: {supported})")

    m = store.load_manifest()
    validate_pdf_source_format(m)
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
    template_source = source_path
    meta = m.get("meta") or {}
    if out_path is not None:
        validate_output_path(store, m, source_path, out_path)
    if MINERU_EPUB_DIGEST in meta and out_format in {"epub", "html"}:
        template_source = cached_epub_source(
            store.source_dir, m["source_sha256"], meta[MINERU_EPUB_DIGEST]
        )
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
            template_source,
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
            source_path=source_path,
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
    if uses_epub_template(m["fmt"], meta):
        writer = _assemble_epub
    elif m["fmt"] == "html":
        writer = _build_epub_from_html_templates
    else:
        writer = _build_epub_from_chapters
    collected_warnings = warnings if warnings is not None else []
    result = writer(
        view,
        template_source,
        out_path,
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
        about=about,
        warnings=collected_warnings,
    )
    if collected_warnings:
        for message in collected_warnings:
            logger.warning("%s", message)
        _write_warnings_sidecar(out_path, collected_warnings)
    return result
