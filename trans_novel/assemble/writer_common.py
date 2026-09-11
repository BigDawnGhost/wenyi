"""Cross-format helpers for output paths, titles, languages and paragraph merging."""

from __future__ import annotations

import os
import re

from ..i18n.languages import normalize_language, require_language
from ..ingest.models import Chapter

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')

_OUT_EXT = {
    "epub": ".epub",
    "txt": ".txt",
    "html": ".html",
    "markdown": ".md",
    "pdf": ".pdf",
    "docx": ".docx",
}


def default_output_format(manifest: dict) -> str:
    """Select the export format from the backend recorded in the same state snapshot."""
    raw_meta = manifest.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    if meta.get("pdf_export") == "babeldoc" or meta.get("babeldoc"):
        return "pdf"
    return "epub"


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


def bilingual_out_path(out_path: str) -> str:
    """Derive the bilingual path from an explicit out_path by appending -bi to the stem."""
    base, ext = os.path.splitext(out_path)
    return f"{base}-bi{ext}"


def _ch_title(c: dict) -> str:
    """Prefer a translated chapter display title, falling back to the original."""
    return (c.get("title_translated") or c.get("title") or "").strip()


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
    """Return a nonempty translation, falling back to source to prevent content loss."""
    return seg.target if (seg.target and seg.target.strip()) else seg.source


def _epub_lang(lang: str | None) -> str:
    """Return the EPUB metadata language code; the default Chinese target is Simplified
    Chinese.
    """
    normalized = normalize_language(lang or "zh")
    if normalized == "zh":
        return "zh-Hans"
    return normalized or (lang or "zh-Hans").replace("_", "-")


def _merged_paragraphs(chapter: Chapter) -> list[tuple[str, str, str]]:
    """Merge chapter segments and continuations into (kind, target, source) paragraph tuples."""
    paras: list[list[str]] = []  # Translation fragments accumulated for each paragraph.
    srcs: list[list[str]] = []  # Source fragments accumulated for each paragraph.
    kinds: list[str] = []
    for s in chapter.segments:
        if not s.source.strip():
            continue
        if s.cont and paras:
            paras[-1].append(_seg_text(s))
            srcs[-1].append(s.source)
        else:
            paras.append([_seg_text(s)])
            srcs.append([s.source])
            kinds.append(s.kind)
    return [(k, "".join(p), "".join(sr)) for k, p, sr in zip(kinds, paras, srcs)]


def _bilingual_source(source: str, target: str) -> str:
    """Omit bilingual source text when blank or identical to a source-fallback translation.
    Strip pronunciation hint markers from Segment.source in plain-text fallbacks. Preserve
    actual ruby from template DOM through _bilingual_source_markup.
    """
    from ..ingest.epub_reader import strip_ruby_markers

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
