"""PDF ingest via external BabelDOC bridge (HTTP only).

Builds a Wenyi Document whose segments carry ``meta.babeldoc_id`` for fillback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..pdf_bridge import BabeldocBridgeClient, BabeldocBridgeError
from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

BABELDOC_META = "babeldoc"
BABELDOC_ID_META = "babeldoc_id"


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
    """Call bridge ``/extract`` and map paragraphs to a single-chapter Document."""
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
    segments: list[Segment] = []
    for index, para in enumerate(paragraphs):
        if not isinstance(para, dict):
            continue
        source = str(para.get("source") or "").strip()
        pid = str(para.get("id") or "").strip()
        if not source or not pid:
            continue
        layout = para.get("layout_label")
        kind = KIND_HEADING if layout == "title" else KIND_TEXT
        segments.append(
            Segment(
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
        )

    chapter = Chapter(index=0, title=title, segments=segments, meta={BABELDOC_META: True})
    return Document(
        title=title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="pdf",
        source_path=str(Path(path).resolve()),
        chapters=[chapter],
        meta={
            BABELDOC_META: True,
            "babeldoc_session_id": session_id,
            "babeldoc_bridge_url": bridge_url.rstrip("/"),
            "babeldoc_pages": pages,
            "pdf_export": "babeldoc",
        },
    )


def translations_from_store(store) -> tuple[str, str, dict[str, str]]:
    """Collect ``{babeldoc_id: target}`` from a RunStore; return session/url/map."""
    manifest = store.load_manifest()
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    session_id = meta.get("babeldoc_session_id")
    bridge_url = meta.get("babeldoc_bridge_url")
    if not session_id or not bridge_url:
        # Fallback: scan chapter/document meta persisted on chapters.
        for info in manifest.get("chapters") or []:
            chapter = store.load_chapter(info["index"])
            # no session on chapter; keep looking in segment-less case
            _ = chapter
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
