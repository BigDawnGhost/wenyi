"""Layout-preserving PDF output through the external BabelDOC HTTP bridge."""

import hashlib
import math
from io import BytesIO
from pathlib import Path
from typing import Literal

from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.errors import PdfReadError

from ..atomicio import publish_target
from ..ingest.pdf_babeldoc import (
    _selected_page_indices,
    annotations_from_store,
    translations_from_store,
)
from ..pdf_bridge import BabeldocBridgeClient, BabeldocBridgeError
from .export_view import AssembleStore
from .pdf_links import merge_page_with_links


def _page_layout(page: PageObject) -> tuple[float, float, Transformation]:
    """Normalize the visible box, rotation and physical units for content and annotations."""
    unit = float(page.user_unit)
    box = page.cropbox
    transform = Transformation().rotate(-page.rotation).scale(unit)
    corners = [
        transform.apply_on(point)
        for point in (box.lower_left, box.lower_right, box.upper_left, box.upper_right)
    ]
    left, bottom = min(x for x, _ in corners), min(y for _, y in corners)
    width = max(x for x, _ in corners) - left
    height = max(y for _, y in corners) - bottom
    if not all(math.isfinite(value) and value > 0 for value in (unit, width, height)):
        raise BabeldocBridgeError("Cannot pair PDF pages with invalid dimensions")
    return width, height, transform.translate(-left, -bottom)


def _write_spreads(
    source: PdfReader,
    translated: PdfReader,
    indices: list[int],
    path: Path,
    *,
    order: Literal["target_first", "source_first"],
) -> None:
    """Pair matching pages left to right in the requested order, without rasterizing."""
    if not indices or len(translated.pages) != len(indices):
        raise BabeldocBridgeError(
            f"Cannot pair bilingual PDF pages: expected {len(indices)} translated pages "
            f"for the saved page selection, received {len(translated.pages)}. "
            "Check the original PDF and BabelDOC session before exporting again."
        )
    with PdfWriter() as writer:
        pairs = []
        source_destinations = {}
        target_destinations = {}
        for target_index, (index, target) in enumerate(zip(indices, translated.pages, strict=True)):
            original = source.pages[index]
            source_width, source_height, source_transform = _page_layout(original)
            target_width, target_height, target_transform = _page_layout(target)
            height = max(source_height, target_height)
            spread = writer.add_blank_page(width=source_width + target_width, height=height)
            source_transform = source_transform.translate(
                tx=target_width if order == "target_first" else 0, ty=height - source_height
            )
            target_transform = target_transform.translate(
                tx=source_width if order == "source_first" else 0, ty=height - target_height
            )
            source_destinations[index] = (spread, source_transform, original)
            target_destinations[target_index] = (spread, target_transform, target)
            pairs.append((spread, original, target, source_transform, target_transform))
        for spread, original, target, source_transform, target_transform in pairs:
            parts = (
                (original, source_transform, source, source_destinations),
                (target, target_transform, translated, target_destinations),
            )
            for page, transform, reader, destinations in (
                parts if order == "source_first" else reversed(parts)
            ):
                merge_page_with_links(writer, spread, page, transform, reader, destinations)
        writer.write(path)


def _assemble_pdf(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    timeout: float,
    bilingual: bool = False,
    order: Literal["target_first", "source_first"] = "target_first",
) -> str:
    """Fill back translations, placing the first bilingual version on the left."""
    session_id, bridge_url, mapping = translations_from_store(store)
    annotations = annotations_from_store(store, mapping)
    annotation_options = {"annotations": annotations} if annotations else {}
    client = BabeldocBridgeClient(bridge_url, timeout=timeout)
    if not bilingual:
        return client.fillback(session_id, mapping, out_path=out_path, **annotation_options)

    try:
        manifest = store.load_manifest()
        data = Path(source_path).read_bytes()
        if manifest.get("source_sha256") != hashlib.sha256(data).hexdigest():
            raise BabeldocBridgeError(
                "PDF source does not match translation state: restore the original PDF "
                "before exporting a bilingual edition."
            )
        source = PdfReader(BytesIO(data))
        meta = manifest.get("meta") or {}
        indices = _selected_page_indices(meta.get("babeldoc_pages"), len(source.pages))
        if not indices:
            raise BabeldocBridgeError("The saved BabelDOC page selection contains no PDF pages")
        # Keep both the bridge result and any failed composition private until the spread is complete.
        with publish_target(out_path) as temp:
            client.fillback(session_id, mapping, out_path=temp, **annotation_options)
            _write_spreads(source, PdfReader(temp), indices, temp, order=order)
    except (OSError, PdfReadError, ValueError) as error:
        raise BabeldocBridgeError(f"Cannot assemble bilingual PDF: {error}") from error
    return out_path
