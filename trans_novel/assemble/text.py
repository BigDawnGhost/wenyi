"""Plain-text output rendering."""

from __future__ import annotations

from trans_novel.ingest import KIND_HEADING, Chapter
from trans_novel.postprocess.punct import normalize_heading_numbering


def merged_paragraphs(chapter: Chapter) -> list[tuple[str, str, str]]:
    """Merge continuation segments into paragraphs."""
    paras: list[list[str]] = []
    srcs: list[list[str]] = []
    kinds: list[str] = []
    preserved: list[bool] = []
    for segment in chapter.segments:
        if not segment.source.strip():
            continue
        target = (
            segment.source
            if segment.preserve_source
            else segment.target
            if segment.target and segment.target.strip()
            else segment.source
        )
        if segment.cont and paras:
            paras[-1].append(target)
            srcs[-1].append(segment.source)
            preserved[-1] = preserved[-1] and segment.preserve_source
        else:
            paras.append([target])
            srcs.append([segment.source])
            kinds.append(segment.kind)
            preserved.append(segment.preserve_source)
    return [
        (
            kind,
            (
                normalize_heading_numbering("".join(target))
                if kind == KIND_HEADING and not preserve
                else "".join(target)
            ),
            "".join(source),
        )
        for kind, target, source, preserve in zip(kinds, paras, srcs, preserved, strict=False)
    ]


def bilingual_source(source: str, target: str) -> str:
    """Return distinct non-empty source text for bilingual output."""
    return source if source.strip() and source != target else ""


def assemble_text(
    store, out_path: str, *, bilingual: bool = False, order: str = "target_first"
) -> str:
    manifest = store.load_manifest()
    chapter_blocks: list[str] = []
    for chapter_meta in manifest["chapters"]:
        chapter = store.load_chapter(chapter_meta["index"])
        blocks: list[str] = []
        for kind, target, source in merged_paragraphs(chapter):
            src = (
                bilingual_source(source, target)
                if bilingual and kind != KIND_HEADING and not chapter.preserve_source
                else ""
            )
            if not src:
                blocks.append(target)
            elif order == "source_first":
                blocks.extend((src, target))
            else:
                blocks.extend((target, src))
        chapter_blocks.append("\n\n".join(blocks))
    with open(out_path, "w", encoding="utf-8") as output:
        output.write("\n\n".join(chapter_blocks) + "\n")
    return out_path
