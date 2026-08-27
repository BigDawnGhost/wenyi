"""PDF ingest via external BabelDOC bridge (HTTP only).

Builds a Wenyi Document whose segments carry ``meta.babeldoc_id`` for fillback.
Chapters are split by the PDF outline (TOC bookmarks) via pypdf.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..pdf_bridge import BabeldocBridgeClient, BabeldocBridgeError
from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

BABELDOC_META = "babeldoc"
BABELDOC_ID_META = "babeldoc_id"


def toc_chapter_starts(pdf_path: str | Path) -> list[tuple[int, str]]:
    """Read PDF bookmarks and return ``(0-based page, title)`` chapter starts.

    Prefer level-2 entries when present (e.g. CHAPTER under PART); otherwise
    level-1. Same page keeps the deeper title. Uses pypdf only (MIT path).
    """
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise BabeldocBridgeError(
            "读取 PDF TOC 需要 pypdf（项目已声明依赖）。请先 uv sync。"
        ) from error

    reader = PdfReader(str(pdf_path))
    flat: list[tuple[int, str, int]] = []  # depth, title, page0

    def walk(items, depth: int) -> None:
        for item in items or []:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            title = getattr(item, "title", None)
            if not isinstance(title, str) or not title.strip():
                continue
            try:
                page0 = reader.get_destination_page_number(item)
            except Exception:
                continue
            if not isinstance(page0, int) or page0 < 0:
                continue
            flat.append((depth, title.strip(), page0))

    walk(reader.outline, 0)
    if not flat:
        return []

    # pypdf nesting: top entries depth 0; prefer depth<=1 when subsections exist.
    has_level1 = any(depth >= 1 for depth, _t, _p in flat)
    max_depth = 1 if has_level1 else 0
    by_page: dict[int, tuple[int, str]] = {}
    for depth, title, page0 in flat:
        if depth > max_depth:
            continue
        previous = by_page.get(page0)
        if previous is None or depth >= previous[0]:
            by_page[page0] = (depth, title)

    return sorted((page0, title) for page0, (_depth, title) in by_page.items())


def _paragraph_page(para: dict) -> int | None:
    page = para.get("page")
    if isinstance(page, int) and page >= 0:
        return page
    pid = str(para.get("id") or "")
    match = re.match(r"^(\d+):", pid)
    if match:
        return int(match.group(1))
    return None


def _assign_chapter_index(page: int, starts: list[tuple[int, str]]) -> int:
    """Latest TOC start with start_page <= page; 0 if before first bookmark."""
    index = 0
    for i, (start_page, _title) in enumerate(starts):
        if page >= start_page:
            index = i
        else:
            break
    return index


def _chapters_from_paragraphs(
    paragraphs: list[dict],
    *,
    book_title: str,
    toc_starts: list[tuple[int, str]],
) -> list[Chapter]:
    if not toc_starts:
        segments: list[Segment] = []
        for index, para in enumerate(paragraphs):
            segment = _segment_from_para(index, para)
            if segment is not None:
                segments.append(segment)
        for i, segment in enumerate(segments):
            segment.index = i
        return [Chapter(index=0, title=book_title, segments=segments, meta={BABELDOC_META: True})]

    buckets: list[list[Segment]] = [[] for _ in toc_starts]
    # Paras before the first TOC page still go into chapter 0.
    for para in paragraphs:
        if not isinstance(para, dict):
            continue
        page = _paragraph_page(para)
        if page is None:
            page = toc_starts[0][0]
        chapter_i = _assign_chapter_index(page, toc_starts)
        segment = _segment_from_para(len(buckets[chapter_i]), para)
        if segment is not None:
            buckets[chapter_i].append(segment)

    chapters: list[Chapter] = []
    out_index = 0
    for (start_page, title), segs in zip(toc_starts, buckets, strict=True):
        if not segs:
            continue
        for i, segment in enumerate(segs):
            segment.index = i
        chapters.append(
            Chapter(
                index=out_index,
                title=title,
                segments=segs,
                meta={
                    BABELDOC_META: True,
                    "pdf_toc_page": start_page,
                    "pdf_toc_title": title,
                },
            )
        )
        out_index += 1

    if not chapters:
        return _chapters_from_paragraphs(paragraphs, book_title=book_title, toc_starts=[])
    return chapters


def _segment_from_para(index: int, para: dict) -> Segment | None:
    source = str(para.get("source") or "").strip()
    pid = str(para.get("id") or "").strip()
    if not source or not pid:
        return None
    layout = para.get("layout_label")
    kind = KIND_HEADING if layout == "title" else KIND_TEXT
    return Segment(
        index=index,
        source=source,
        kind=kind,
        meta={
            BABELDOC_ID_META: pid,
            "layout_label": layout,
            "debug_id": para.get("debug_id"),
            "page": para.get("page"),
            "para_index": para.get("index"),
        },
    )


def read_pdf_babeldoc(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    bridge_url: str,
    pages: str | None = None,
    cache_dir: str | None = None,
    timeout: float = 600.0,
) -> Document:
    """Call bridge ``/extract`` and map paragraphs into TOC-based chapters."""
    client = BabeldocBridgeClient(bridge_url, timeout=timeout)
    payload = client.extract(path, pages=pages)
    session_id = payload.get("session_id")
    paragraphs_doc = payload.get("paragraphs") or {}
    paragraphs = paragraphs_doc.get("paragraphs") or []
    if not isinstance(session_id, str) or not session_id:
        raise BabeldocBridgeError("bridge extract 未返回 session_id")
    if not isinstance(paragraphs, list) or not paragraphs:
        raise BabeldocBridgeError("bridge extract 未返回段落")

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "babeldoc_extract.json")
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    title = Path(path).stem
    try:
        toc_starts = toc_chapter_starts(path)
    except BabeldocBridgeError:
        toc_starts = []
    except Exception:
        toc_starts = []

    chapters = _chapters_from_paragraphs(
        [p for p in paragraphs if isinstance(p, dict)],
        book_title=title,
        toc_starts=toc_starts,
    )

    return Document(
        title=title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="pdf",
        source_path=str(Path(path).resolve()),
        chapters=chapters,
        meta={
            BABELDOC_META: True,
            "babeldoc_session_id": session_id,
            "babeldoc_bridge_url": bridge_url.rstrip("/"),
            "babeldoc_pages": pages,
            "pdf_export": "babeldoc",
            "pdf_toc_chapters": len(toc_starts),
        },
    )


def translations_from_store(store) -> tuple[str, str, dict[str, str]]:
    """Collect ``{babeldoc_id: target}`` from a RunStore; return session/url/map."""
    manifest = store.load_manifest()
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    session_id = meta.get("babeldoc_session_id")
    bridge_url = meta.get("babeldoc_bridge_url")
    if not session_id or not bridge_url:
        raise BabeldocBridgeError(
            "状态中缺少 babeldoc_session_id / babeldoc_bridge_url；"
            "请用 pdf_backend=babeldoc 重新解析，并保持 bridge 进程不退出。"
        )

    mapping: dict[str, str] = {}
    for info in manifest.get("chapters") or []:
        chapter = store.load_chapter(info["index"])
        for segment in chapter.segments:
            pid = None
            if isinstance(segment.meta, dict):
                pid = segment.meta.get(BABELDOC_ID_META)
            if not pid:
                continue
            text = segment.target if segment.target is not None else segment.source
            if text is None or not str(text).strip():
                continue
            mapping[str(pid)] = str(text)
    if not mapping:
        raise BabeldocBridgeError("没有带 babeldoc_id 的译文可回填")
    return str(session_id), str(bridge_url), mapping
