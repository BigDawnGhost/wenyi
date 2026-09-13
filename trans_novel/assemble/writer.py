"""输出路径、格式分派与统一发布入口。"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Sequence
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from trans_novel.assemble.epub.rendering.generated import build_epub_from_chapters
from trans_novel.assemble.epub.rendering.source_archive import assemble_epub
from trans_novel.assemble.text import assemble_text
from trans_novel.epub.slots import distribute_slot_translation

if TYPE_CHECKING:
    from trans_novel.assemble.epub.rendering.theme.service import ThemeService
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
    theme: ThemeService | None = None,
) -> dict[str, Any]:
    """用临时译文预检 EPUB，不调用模型或写入运行状态。"""
    doc = doc.model_copy(deep=True)
    for chapter in doc.chapters:
        for segment in chapter.segments:
            if segment.preserve_source or not segment.source.strip():
                continue
            marker = f"预检译文 {chapter.index}-{segment.index}"
            segment.assign_translation(
                distribute_slot_translation(segment.epub_state, marker)
                if segment.epub_state is not None
                else marker
            )
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
        source_backed = doc.fmt == "epub"
        renderer = assemble_epub if source_backed else build_epub_from_chapters
        plan = renderer(store, source_path, output, bilingual=bilingual, order=order, theme=theme)
        from trans_novel.assemble.epub.verification import verify_epub

        return verify_epub(
            output,
            source_path=source_path if source_backed else None,
            store=store,
            mode=("bilingual" if bilingual else "monolingual") if source_backed else "generated",
            bilingual=bilingual,
            bilingual_order=order,
            theme_plan=plan,
        )


def assemble_outputs(
    store: RunStore,
    source_path: str,
    outputs: Sequence[tuple[str | None, bool]],
    out_format: str = "epub",
    *,
    order: str = "target_first",
    theme: ThemeService | None = None,
    output_digest: str | None = None,
) -> list[str]:
    """一次生成所有指定输出；每份 EPUB 验证通过后才开始发布。"""
    if not outputs:
        return []
    resolved = [
        (path or _default_out(source_path, out_format, "", bilingual=bilingual), bilingual)
        for path, bilingual in outputs
    ]
    for path, _ in resolved:
        _reject_output_alias(source_path, path)
    if order not in {"target_first", "source_first"}:
        raise ValueError(f"invalid bilingual order: {order!r}")
    from trans_novel.pipeline.execution import ensure_assemble_ready

    ensure_assemble_ready(store, source_path)
    if out_format == "txt":
        return [
            assemble_text(store, path, bilingual=bilingual, order=order)
            for path, bilingual in resolved
        ]
    from trans_novel.assemble.epub.publication import EpubOutput, publish_epubs

    source_backed = store.load_manifest()["fmt"] == "epub"
    renderer = assemble_epub if source_backed else build_epub_from_chapters
    requests = [
        EpubOutput(
            path,
            ("bilingual" if bilingual else "monolingual") if source_backed else "generated",
            bilingual,
            partial(renderer, store, source_path, bilingual=bilingual, order=order, theme=theme),
        )
        for path, bilingual in resolved
    ]
    return publish_epubs(
        store,
        source_path if source_backed else None,
        requests,
        bilingual_order=order,
        source_identity_path=source_path,
        output_digest=output_digest,
    )


def assemble(
    store: RunStore,
    source_path: str,
    out_path: str | None = None,
    out_format: str = "epub",
    *,
    bilingual: bool = False,
    order: str = "target_first",
    theme: ThemeService | None = None,
    output_digest: str | None = None,
) -> str:
    """生成一份输出。"""
    return assemble_outputs(
        store,
        source_path,
        [(out_path, bilingual)],
        out_format,
        order=order,
        theme=theme,
        output_digest=output_digest,
    )[0]
