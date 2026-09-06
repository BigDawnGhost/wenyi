"""Public output path and format dispatch facade."""

from __future__ import annotations

import os
import re
import tempfile
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from trans_novel.assemble.epub.rendering.generated import build_epub_from_chapters
from trans_novel.assemble.epub.rendering.source_archive import assemble_epub
from trans_novel.assemble.text import assemble_text
from trans_novel.epub.slots import distribute_slot_translation

if TYPE_CHECKING:
    from trans_novel.ingest.models import Document
    from trans_novel.pipeline.state import RunStore

_ILLEGAL_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _sanitize_filename(name: str, fallback: str = "translated") -> str:
    name = _ILLEGAL_FN.sub(" ", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:120] or fallback


def _default_out(
    source_path: str, out_format: str, title: str | None = None, *, bilingual: bool = False
) -> str:
    ext = ".epub" if out_format == "epub" else ".txt"
    if title and title.strip():
        directory = os.path.dirname(os.path.abspath(source_path))
        return os.path.join(directory, _sanitize_filename(title) + ext)
    base, _ = os.path.splitext(source_path)
    suffix = ".zh-bi" if bilingual else ".zh"
    return f"{base}{suffix}{ext}"


def bilingual_out_path(out_path: str) -> str:
    """Derive the bilingual path from an explicitly supplied output path."""
    base, ext = os.path.splitext(out_path)
    return f"{base}-bi{ext}"


def _reject_output_alias(source_path: str, out_path: str) -> None:
    source = os.path.abspath(os.fspath(source_path))
    output = os.path.abspath(os.fspath(out_path))
    try:
        if os.path.realpath(source) == os.path.realpath(output):
            raise ValueError("input and output paths must differ")
        if os.path.exists(source) and os.path.exists(output) and os.path.samefile(source, output):
            raise ValueError("input and output paths must differ")
    except OSError:
        return


def preflight_epub(
    doc: Document,
    source_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> dict[str, Any]:
    """Render and verify the source-backed output before paid translation work starts."""
    doc = doc.model_copy(deep=True)
    for chapter in doc.chapters:
        for segment in chapter.segments:
            if segment.epub_state is not None:
                marker = f"预检译文 {chapter.index}-{segment.index}"
                segment.assign_translation(distribute_slot_translation(segment.epub_state, marker))
    manifest = {
        "fmt": doc.fmt,
        "meta": doc.meta,
        "source_lang": doc.source_lang,
        "target_lang": doc.target_lang,
        "chapters": [{"index": chapter.index} for chapter in doc.chapters],
    }
    chapters = {chapter.index: chapter for chapter in doc.chapters}
    store = SimpleNamespace(
        load_manifest=lambda: manifest,
        load_chapter=chapters.__getitem__,
    )
    with tempfile.TemporaryDirectory() as directory:
        output = os.path.join(directory, "preflight.epub")
        assemble_epub(store, source_path, output, bilingual=bilingual, order=order)
        from trans_novel.assemble.epub.verification import verify_epub

        return verify_epub(
            output,
            source_path=source_path,
            store=store,
            mode="bilingual" if bilingual else "monolingual",
            bilingual=bilingual,
            bilingual_order=order,
        )


def assemble(
    store: RunStore,
    source_path: str,
    out_path: str | None = None,
    out_format: str = "epub",
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """Generate translated output (EPUB by default)."""
    manifest = store.load_manifest()
    if out_format == "txt":
        out_path = out_path or _default_out(source_path, "txt", "", bilingual=bilingual)
    else:
        out_path = out_path or _default_out(source_path, "epub", "", bilingual=bilingual)
    _reject_output_alias(source_path, out_path)
    if order not in {"target_first", "source_first"}:
        raise ValueError(f"invalid bilingual order: {order!r}")
    from trans_novel.pipeline.execution import ensure_assemble_ready

    ensure_assemble_ready(store, source_path)
    if out_format == "txt":
        return assemble_text(store, out_path, bilingual=bilingual, order=order)
    from trans_novel.assemble.epub import publish_epub

    if manifest["fmt"] == "epub":
        mode = "bilingual" if bilingual else "monolingual"
        return publish_epub(
            store,
            source_path,
            out_path,
            mode=mode,
            bilingual=bilingual,
            bilingual_order=order,
            writer=lambda temp_path: assemble_epub(
                store, source_path, temp_path, bilingual=bilingual, order=order
            ),
        )
    return publish_epub(
        store,
        None,
        out_path,
        mode="generated",
        bilingual=bilingual,
        bilingual_order=order,
        writer=lambda temp_path: build_epub_from_chapters(
            store, source_path, temp_path, bilingual=bilingual, order=order
        ),
        source_identity_path=source_path,
    )
