"""PDF ingest via external BabelDOC bridge (HTTP only).

Builds a Wenyi Document whose segments carry ``meta.babeldoc_id`` for fillback.
Chapters are split by the PDF outline (TOC bookmarks) via pypdf.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..alignment.objects import object_marker, resolve_objects
from ..markup.objects import OBJECT_RELATION, object_anchors
from ..pdf_bridge import BabeldocBridgeClient, BabeldocBridgeError
from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

BABELDOC_META = "babeldoc"
BABELDOC_ID_META = "babeldoc_id"


def _selected_page_indices(pages: str | None, page_count: int) -> list[int]:
    """Expand BabelDOC's 1-based page selection syntax into 0-based indices."""
    if not pages or not pages.strip():
        return list(range(page_count))

    selected: set[int] = set()
    try:
        for raw_token in pages.split(","):
            token = raw_token.strip()
            if not token:
                continue
            if token.isdigit():
                page = int(token)
                if 1 <= page <= page_count:
                    selected.add(page - 1)
                continue

            match = re.fullmatch(r"(\d*)-(\d*)", token)
            if match is None or not any(match.groups()):
                return []
            start = int(match.group(1)) if match.group(1) else 1
            end = int(match.group(2)) if match.group(2) else page_count
            if start < 1 or end < 1 or start > end:
                return []
            selected.update(range(max(1, start) - 1, min(page_count, end)))
    except ValueError:
        return []
    return sorted(selected)


def _resolved_pdf_object(value: Any) -> Any:
    get_object = getattr(value, "get_object", None)
    return get_object() if callable(get_object) else value


def _resources_have_image(resources: Any, *, seen: set[int] | None = None) -> bool:
    """Return whether PDF resources contain an image, including nested forms."""
    resources = _resolved_pdf_object(resources)
    if not hasattr(resources, "get"):
        return False
    if seen is None:
        seen = set()
    identity = id(resources)
    if identity in seen:
        return False
    seen.add(identity)

    xobjects = _resolved_pdf_object(resources.get("/XObject"))
    if not hasattr(xobjects, "values"):
        return False
    for reference in xobjects.values():
        xobject = _resolved_pdf_object(reference)
        if not hasattr(xobject, "get"):
            continue
        subtype = str(xobject.get("/Subtype") or "")
        if subtype == "/Image":
            return True
        if subtype == "/Form" and _resources_have_image(xobject.get("/Resources"), seen=seen):
            return True
    return False


def _page_has_image(page: Any) -> bool:
    if _resources_have_image(page.get("/Resources")):
        return True
    try:
        content = page.get_contents()
        return content is not None and any(
            operator == b"INLINE IMAGE" for _operands, operator in content.operations
        )
    except Exception:
        return False


def _is_image_only_pdf(path: str | Path, *, pages: str | None = None) -> bool:
    """Best-effort detection of selected pages containing images but no text layer.

    Detection failures deliberately fall through to BabelDOC, which remains the
    authority for parsing unusual or damaged PDFs.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        indices = _selected_page_indices(pages, len(reader.pages))
        if not indices:
            return False

        found_image = False
        for index in indices:
            page = reader.pages[index]
            text = page.extract_text() or ""
            if text.strip():
                return False
            found_image = found_image or _page_has_image(page)
        return found_image
    except Exception:
        return False


def toc_chapter_starts(pdf_path: str | Path) -> list[tuple[int, str]]:
    """Read PDF bookmarks and return ``(0-based page, title)`` chapter starts.

    Prefer level-2 entries when present (e.g. CHAPTER under PART); otherwise
    level-1. Same page keeps the deeper title. Uses pypdf only, without importing BabelDOC.
    """
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise BabeldocBridgeError(
            "Reading the PDF TOC requires pypdf, a declared dependency. Run uv sync first."
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
        for para in paragraphs:
            segments.extend(_segments_from_para(len(segments), para))
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
        buckets[chapter_i].extend(_segments_from_para(len(buckets[chapter_i]), para))

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


def _segments_from_para(index: int, para: dict) -> list[Segment]:
    """Translate directory rows independently while retaining the original parent ID."""
    rows = para.get("rows")
    if not rows:
        segment = _segment_from_para(index, para)
        return [segment] if segment is not None else []
    source = str(para.get("source") or "")
    if not isinstance(rows, list) or len(rows) < 2:
        raise BabeldocBridgeError("Invalid BabelDOC directory rows; update the bridge and reparse.")
    segments = []
    cursor = 0
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BabeldocBridgeError("Invalid BabelDOC directory row boundaries.")
        start, end = row.get("source_start"), row.get("source_end")
        if (
            type(start) is not int
            or type(end) is not int
            or not cursor <= start < end <= len(source)
            or source[cursor:start].strip()
            or not source[start:end].strip()
        ):
            raise BabeldocBridgeError("Invalid BabelDOC directory row boundaries.")
        items = []
        for item in para.get("annotations") or []:
            left, right = item["source_start"], item["source_end"]
            if start <= left <= right <= end:
                items.append({**item, "source_start": left - start, "source_end": right - start})
            elif left < end and start < right:
                raise BabeldocBridgeError("BabelDOC annotation crosses a directory row.")
        segment = _segment_from_para(
            index + row_index, {**para, "source": source[start:end], "annotations": items}
        )
        if segment is None:
            raise BabeldocBridgeError("BabelDOC directory row is missing its parent identity.")
        segment.meta["babeldoc_row"] = {"index": row_index, "count": len(rows)}
        segments.append(segment)
        cursor = end
    if source[cursor:].strip():
        raise BabeldocBridgeError("BabelDOC directory rows leave source text uncovered.")
    return segments


def _segment_from_para(index: int, para: dict) -> Segment | None:
    source = str(para.get("source") or "")
    pid = str(para.get("id") or "").strip()
    if not source.strip() or not pid:
        return None
    layout = para.get("layout_label")
    kind = KIND_HEADING if layout == "title" else KIND_TEXT
    annotations = para.get("annotations")
    metadata: dict[str, Any] = {
        BABELDOC_ID_META: pid,
        "layout_label": layout,
        "debug_id": para.get("debug_id"),
        "page": para.get("page"),
        "para_index": para.get("index"),
    }
    if isinstance(annotations, list) and annotations:
        annotations = deepcopy(annotations)
        for item in annotations:
            if item.get("protected"):
                if item.get("kind") != "formula" or item.get("mode") != "point":
                    raise BabeldocBridgeError("Invalid protected PDF object; update the bridge.")
                key = f"pdf:{pid}:{item['id']}"
                # The PDF's extracted spelling is not mathematical evidence.
                # Keep only an opaque type/identity hint in model context.
                item.update(
                    relation=OBJECT_RELATION,
                    target_key=key,
                    marker=object_marker(key, source),
                    object_tag="PDF mathematics",
                    object_source="Original PDF mathematical glyph object",
                )
                item.pop("reference_quality", None)
        metadata["pdf_annotations"] = {
            "source_length": len(source),
            "items": annotations,
        }
    return Segment(
        index=index,
        source=source,
        kind=kind,
        meta=metadata,
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
    if _is_image_only_pdf(path, pages=pages):
        raise BabeldocBridgeError(
            "BabelDOC found only scanned images with no extractable text layer on the selected PDF pages. "
            "Use the MinerU backend (pipeline.pdf_backend: mineru), "
            "or add a searchable text layer with OCR before using babeldoc."
        )
    client = BabeldocBridgeClient(bridge_url, timeout=timeout)
    payload = client.extract(path, pages=pages)
    session_id = payload.get("session_id")
    paragraphs_doc = payload.get("paragraphs") or {}
    paragraphs = paragraphs_doc.get("paragraphs") or []
    if not isinstance(session_id, str) or not session_id:
        raise BabeldocBridgeError("bridge extract returned no session_id")
    if not isinstance(paragraphs, list) or not paragraphs:
        raise BabeldocBridgeError("bridge extract returned no paragraphs")

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


def _fillback_groups(store) -> dict[str, list[Segment]]:
    """Recover stable row order and reject missing/duplicated row checkpoints."""
    groups: dict[str, list[Segment]] = {}
    for info in store.load_manifest().get("chapters") or []:
        for segment in store.load_chapter(info["index"]).segments:
            pid = str(segment.meta.get(BABELDOC_ID_META) or "")
            if pid:
                groups.setdefault(pid, []).append(segment)
    for pid, segments in groups.items():
        if not any("babeldoc_row" in segment.meta for segment in segments):
            groups[pid] = segments[-1:]
            continue
        indices: list[int] = []
        for segment in segments:
            row = segment.meta.get("babeldoc_row")
            if (
                not isinstance(row, dict)
                or type(row.get("index")) is not int
                or type(row.get("count")) is not int
                or row["count"] != len(segments)
            ):
                raise BabeldocBridgeError(
                    f"Incomplete or duplicate directory rows for {pid}; reparse."
                )
            indices.append(row["index"])
        if sorted(indices) != list(range(len(segments))):
            raise BabeldocBridgeError(f"Incomplete or duplicate directory rows for {pid}; reparse.")
        segments.sort(key=lambda segment: segment.meta["babeldoc_row"]["index"])
    return groups


def _fillback_text(segment: Segment) -> str:
    """Keep source fallback export-only; never turn pending rows into saved targets."""
    text = segment.target
    text = str(text if text is not None and text.strip() else segment.source)
    metadata = segment.meta.get("pdf_annotations", {})
    items = [item for item in metadata.get("items", []) if item.get("protected")]
    if items and not metadata.get("objects_resolved_for_export"):
        resolved = resolve_objects([text], object_anchors(items), segment.source)
        if resolved.damaged or len(resolved.placements) != len(items):
            raise BabeldocBridgeError(
                "Cannot export protected PDF objects with missing or damaged markers. "
                "Repair the translation before exporting."
            )
        return resolved.targets[0]
    return text


def rows_from_store(store, translations: dict[str, str]) -> dict[str, list[dict[str, int]]]:
    """Export explicit target ranges, independent of newlines inside a translated row."""
    result = {}
    for pid, segments in _fillback_groups(store).items():
        if "babeldoc_row" not in segments[0].meta:
            continue
        texts = [_fillback_text(segment) for segment in segments]
        if translations.get(pid) != "\n".join(texts):
            raise BabeldocBridgeError(f"Directory row text changed during export for {pid}.")
        ranges = []
        cursor = 0
        for text in texts:
            ranges.append({"target_start": cursor, "target_end": cursor + len(text)})
            cursor += len(text) + 1
        result[pid] = ranges
    return result


def translations_from_store(store) -> tuple[str, str, dict[str, str]]:
    """Collect fillback text, falling back to source for blank targets without saving it."""
    manifest = store.load_manifest()
    meta = manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {}
    session_id = meta.get("babeldoc_session_id")
    bridge_url = meta.get("babeldoc_bridge_url")
    if not session_id or not bridge_url:
        raise BabeldocBridgeError(
            "State lacks babeldoc_session_id / babeldoc_bridge_url. "
            "Parse again with pdf_backend=babeldoc and keep the bridge process running."
        )

    mapping: dict[str, str] = {}
    for pid, segments in _fillback_groups(store).items():
        text = "\n".join(_fillback_text(segment) for segment in segments)
        if text.strip():
            mapping[pid] = text
    if not mapping:
        raise BabeldocBridgeError(
            "No translated paragraphs with babeldoc_id are available for backfill"
        )
    return str(session_id), str(bridge_url), mapping


def annotations_from_store(store, translations: dict[str, str]) -> dict[str, Any]:
    """Forward cached positions for per-item validation, without model calls or state changes."""
    result: dict[str, Any] = {}
    for pid, segments in _fillback_groups(store).items():
        if pid not in translations:
            continue
        if "babeldoc_row" in segments[0].meta:
            placements = []
            cursor = 0
            for segment in segments:
                text = _fillback_text(segment)
                payload = _segment_annotations(segment, text)
                if (
                    payload
                    and payload["target_digest"] == hashlib.sha256(text.encode()).hexdigest()
                    and isinstance(payload["placements"], list)
                ):
                    for row in payload["placements"]:
                        if not isinstance(row, dict):
                            continue
                        shifted = deepcopy(row)
                        for key in ("target_start", "target_end"):
                            if type(shifted.get(key)) is int:
                                shifted[key] += cursor
                        placements.append(shifted)
                cursor += len(text) + 1
            if any(segment.meta.get("pdf_annotations") for segment in segments):
                result[pid] = {
                    "target_digest": hashlib.sha256(translations[pid].encode()).hexdigest(),
                    "placements": placements,
                }
        else:
            payload = _segment_annotations(segments[0], translations[pid])
            if payload is not None:
                result[pid] = payload
    return result


def _segment_annotations(segment: Segment, text: str) -> dict[str, Any] | None:
    """Keep local annotation digests intact until a structured export validates them."""
    metadata = segment.meta.get("pdf_annotations")
    if not isinstance(metadata, dict):
        return None
    items = metadata.get("items")
    if not isinstance(items, list) or not items:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    protected = {item["id"] for item in items if item.get("protected")}
    transported = protected and (
        metadata.get("objects_resolved_for_export")
        or (segment.target and segment.target.strip() and segment.target != segment.source)
    )
    if text == segment.source and not transported:
        placements = [
            {
                "id": item["id"],
                "mode": item["mode"],
                "target_start": item["source_start"],
                "target_end": item["source_end"],
                **({"target_order": order} if item.get("protected") else {}),
                "status": "aligned",
                "method": "source_identity",
            }
            for order, item in enumerate(items)
        ]
    else:
        # Keep stale positions invalid rather than blessing them with a new digest.
        digest = metadata.get("target_digest")
        placements = metadata.get("placements", [])
    if protected:
        valid = [
            row["id"]
            for row in placements
            if isinstance(row, dict)
            and row.get("status") == "aligned"
            and row.get("mode") == "point"
            and type(row.get("target_start")) is int
            and row["target_start"] == row.get("target_end")
            and 0 <= row["target_start"] <= len(text)
        ]
        if digest != hashlib.sha256(text.encode()).hexdigest() or any(
            valid.count(item_id) != 1 for item_id in protected
        ):
            raise BabeldocBridgeError(
                "Cannot export protected PDF objects with stale or incomplete alignment. "
                "Resume annotation alignment before exporting."
            )
    return {
        "target_digest": digest,
        "placements": deepcopy(placements),
        **({"protected_objects": True} if protected else {}),
    }
