"""Cross-format helpers for output paths, titles, languages and paragraph merging."""

from __future__ import annotations

import os
import re

from ..export_paths import bilingual_out_path as bilingual_out_path
from ..i18n.languages import require_language
from ..i18n.policy.models import PolicyContext
from ..i18n.policy.resolver import resolve_policy
from ..markup.contracts import HTML_ATTRIBUTE_META_KEY
from ..models.document import Chapter, Segment, uses_epub_template

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')

_OUT_EXT = {
    "epub": ".epub",
    "txt": ".txt",
    "html": ".html",
    "markdown": ".md",
    "pdf": ".pdf",
    "docx": ".docx",
}


def supports_pdf_export(manifest: dict) -> bool:
    """Only persisted BabelDOC state can be filled back into the original PDF layout."""
    raw_meta = manifest.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    return meta.get("pdf_export") == "babeldoc" or bool(meta.get("babeldoc"))


def validate_pdf_source_format(manifest: dict) -> None:
    """Require re-import for projects whose only template was a MinerU HTML export."""
    if (
        manifest.get("fmt") == "pdf"
        and not uses_epub_template("pdf", manifest.get("meta"))
        and not supports_pdf_export(manifest)
    ):
        raise ValueError(
            "This MinerU project uses the retired HTML format. Re-import the PDF as a new project."
        )


def output_formats(manifest: dict) -> list[str]:
    """List book export formats supported by this saved state, not current configuration."""
    return [fmt for fmt in _OUT_EXT if fmt != "pdf" or supports_pdf_export(manifest)]


def output_backend(manifest: dict, fmt: str) -> str:
    """Resolve the writer backend and reject unsupported PDF output before any writes."""
    if fmt != "pdf":
        return "native"
    if not supports_pdf_export(manifest):
        raise ValueError(
            "PDF export is only available for BabelDOC projects; use EPUB, DOCX or HTML instead."
        )
    return "babeldoc"


def default_output_format(manifest: dict) -> str:
    """Select the export format from the backend recorded in the same state snapshot."""
    if manifest.get("fmt") == "html":
        return "html"
    return "pdf" if supports_pdf_export(manifest) else "epub"


def _sanitize_filename(name: str, fallback: str = "translated") -> str:
    """Remove characters invalid in cross-platform filenames and limit name length."""
    name = _ILLEGAL_FN.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def _ensure_parent_dir(path: str) -> None:
    """Create the output directory while allowing a bare filename."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def _default_out(
    source_path: str,
    out_format: str,
    title: str | None = None,
    *,
    bilingual: bool = False,
    target_lang: str = "zh",
) -> str:
    """Return the default export path under the input file's ``output`` folder."""
    ext = _OUT_EXT.get(out_format, ".epub")
    output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), "output")
    os.makedirs(output_dir, exist_ok=True)
    if title and title.strip():
        # Available for explicit callers; default assembly does not pass a translated book title.
        return os.path.join(output_dir, _sanitize_filename(title) + ext)
    base, _ = os.path.splitext(source_path)
    suffix = f".{require_language(target_lang)}{'-bi' if bilingual else ''}"
    return os.path.join(
        output_dir,
        f"{os.path.basename(base)}{suffix}{ext}",
    )


def _ch_title(c: dict) -> str:
    """Prefer a translated chapter display title, falling back to the original."""
    return (c.get("title_translated") or c.get("title") or "").strip()


def _translated_book_title(manifest: dict) -> str:
    """Resolve a translated book title from manifest metadata or chapter 0 when available."""
    direct = manifest.get("title_translated")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    meta = manifest.get("meta")
    if isinstance(meta, dict):
        meta_title = meta.get("book_title_translated")
        if isinstance(meta_title, str) and meta_title.strip():
            return meta_title.strip()
    source_title = manifest.get("title")
    chapters = manifest.get("chapters")
    if (
        isinstance(source_title, str)
        and source_title.strip()
        and isinstance(chapters, list)
        and chapters
        and isinstance(chapters[0], dict)
    ):
        first = chapters[0]
        if (
            isinstance(first.get("title"), str)
            and " ".join(first["title"].split()) == " ".join(source_title.split())
            and isinstance(first.get("title_translated"), str)
            and first["title_translated"].strip()
        ):
            return first["title_translated"].strip()
    return ""


def _resolve_export_title(
    manifest: dict,
    target_lang: str | None,
    *,
    bilingual: bool,
) -> tuple[str, str]:
    """Return ``(export_title, original_title)``.

    Only native EPUB projects have translated package titles. Other inputs keep the
    standard ``<source>-wenyi-<lang>`` title, even if their first chapter has the same name.
    """
    raw_title = manifest.get("title")
    source_title = raw_title.strip() if isinstance(raw_title, str) else ""
    if manifest.get("fmt") == "epub" and (translated := _translated_book_title(manifest)):
        return translated, source_title
    return _export_book_title(source_title or "translated", target_lang, bilingual=bilingual), ""


def _export_book_title(
    title: str | None,
    target_lang: str | None,
    *,
    bilingual: bool,
) -> str:
    """Append Wenyi, the target language and optional bilingual marker to the original book
    title.
    """
    base = (title or "").strip() or "translated"
    lang = (target_lang or "").strip().replace("_", "-").lower() or "zh"
    suffix = f"-wenyi-{lang}{'-bi' if bilingual else ''}"
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def _seg_text(seg) -> str:
    """Return the saved translation (including an intentional empty string), falling back to
    source only when the segment is still untranslated (``target is None``).
    Skipped boilerplate segments always export empty.
    """
    if seg.meta.get("skip"):
        return ""
    return seg.source if seg.target is None else seg.target


def _heading_level(chapter: Chapter, segment: Segment | None = None) -> int:
    """Prefer segment heading metadata, falling back to chapter metadata for older text state."""
    level = chapter.meta.get("heading_level", 1)
    if segment is not None:
        level = segment.meta.get("heading_level", level)
    return level if isinstance(level, int) and not isinstance(level, bool) and level > 0 else 1


def _epub_lang(lang: str | None) -> str:
    """Return the EPUB metadata language code; the default Chinese target is Simplified
    Chinese.
    """
    return resolve_policy(
        PolicyContext("auto", lang or "zh", phase="export", format="epub")
    ).export.language_tag


def _merged_paragraphs(chapter: Chapter) -> list[tuple[str, str, str, int]]:
    """Merge continuations while retaining each paragraph's original heading level."""
    paras: list[list[str]] = []  # Translation fragments accumulated for each paragraph.
    srcs: list[list[str]] = []  # Source fragments accumulated for each paragraph.
    kinds: list[str] = []
    levels: list[int] = []
    attribute_continuation = False
    skip_continuation = False
    for s in chapter.segments:
        if not s.cont:
            attribute_continuation = HTML_ATTRIBUTE_META_KEY in s.meta
            skip_continuation = bool(s.meta.get("skip"))
        if attribute_continuation or skip_continuation or s.meta.get("skip"):
            continue
        if not s.source.strip():
            continue
        if s.cont and paras:
            paras[-1].append(_seg_text(s))
            srcs[-1].append(s.source)
        else:
            paras.append([_seg_text(s)])
            srcs.append([s.source])
            kinds.append(s.kind)
            levels.append(_heading_level(chapter, s))
    return [
        (kind, "".join(target), "".join(source), level)
        for kind, target, source, level in zip(kinds, paras, srcs, levels)
    ]


def _bilingual_source(source: str, target: str) -> str:
    """Omit bilingual source text when blank or identical to a source-fallback translation.
    Strip pronunciation hint markers from Segment.source in plain-text fallbacks. Preserve
    actual ruby from template DOM through _bilingual_source_markup.
    """
    from wenyi_core.markup.ruby import strip_ruby_markers

    # Source fallbacks still include pronunciation markers, so compare before stripping them.
    if source == target or not target.strip():
        return ""
    source = strip_ruby_markers(source)
    return source if (source.strip() and source != target) else ""


def _ordered_pair(source: str, target: str, order: str) -> tuple[str, str]:
    """Return source first for source_first order, otherwise translation first."""
    return (source, target) if order == "source_first" else (target, source)


def _manifest_target_lang(manifest: dict) -> str:
    """Require an explicit supported target language in persisted state."""
    raw = manifest.get("target_lang")
    if not isinstance(raw, str) or not raw:
        raise ValueError("State is missing target_lang; create a new translation.")
    return require_language(raw)
