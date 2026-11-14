"""Remap internal destinations when PDF pages become the halves of bilingual spreads."""

from __future__ import annotations

from copy import copy
from typing import Any, cast

from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NullObject,
)


def _destination(
    raw: Any,
    reader: PdfReader,
    destinations: dict[int, tuple[PageObject, Transformation, PageObject]],
) -> ArrayObject | None:
    if isinstance(raw, str):
        named = reader.named_destinations.get(raw) or reader.named_destinations.get(raw.lstrip("/"))
        raw = named.dest_array if named is not None else None
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    page = raw[0]
    if isinstance(page, int):
        index = int(page)
    elif hasattr(page, "get_object") and getattr(page.get_object(), "indirect_reference", None):
        index = reader.get_page_number(page.get_object())
    else:
        return None
    if index not in destinations:
        return None
    spread, transform, original = destinations[index]
    view = str(raw[1])
    if view in {"/XYZ", "/FitH", "/FitBH", "/FitV", "/FitBV"}:
        left, top = float(original.cropbox.left), float(original.cropbox.top)
        zoom: Any = NullObject()
        if view == "/XYZ":
            if len(raw) > 2 and isinstance(raw[2], (int, float)):
                left = float(raw[2])
            if len(raw) > 3 and isinstance(raw[3], (int, float)):
                top = float(raw[3])
            if len(raw) > 4:
                zoom = raw[4]
        elif len(raw) > 2 and isinstance(raw[2], (int, float)):
            if view in {"/FitH", "/FitBH"}:
                top = float(raw[2])
            else:
                left = float(raw[2])
        x, y = transform.apply_on((left, top))
        return ArrayObject(
            [spread.indirect_reference, NameObject("/XYZ"), FloatObject(x), FloatObject(y), zoom]
        )
    if view == "/FitR" and len(raw) == 6:
        if not all(isinstance(value, (int, float)) for value in raw[2:]):
            return None
        points = [
            transform.apply_on((float(raw[x]), float(raw[y])))
            for x, y in ((2, 3), (2, 5), (4, 3), (4, 5))
        ]
        return ArrayObject(
            [
                spread.indirect_reference,
                NameObject("/FitR"),
                FloatObject(min(x for x, _ in points)),
                FloatObject(min(y for _, y in points)),
                FloatObject(max(x for x, _ in points)),
                FloatObject(max(y for _, y in points)),
            ]
        )
    return ArrayObject([spread.indirect_reference, NameObject("/Fit")])


def merge_page_with_links(
    writer: PdfWriter,
    spread: PageObject,
    page: PageObject,
    transform: Transformation,
    reader: PdfReader,
    destinations: dict[int, tuple[PageObject, Transformation, PageObject]],
) -> None:
    """Keep transformed annotations without cloning obsolete destination page trees."""
    original_annotations = page.get("/Annots") or []
    prepared = copy(page)
    prepared.pop("/Annots", None)
    spread.merge_transformed_page(prepared, transform)
    # Merge content separately: pypdf can silently fall back to untransformed
    # annotations when a sanitized direct dictionary lacks an indirect reference.
    for reference in original_annotations:
        original = reference.get_object()
        annotation = DictionaryObject(original)
        destination = None
        action = original.get("/A")
        action = action.get_object() if action is not None else {}
        if original.get("/Subtype") == "/Link":
            if "/Dest" in original:
                destination = original["/Dest"]
                annotation.pop("/Dest", None)
            elif isinstance(action, dict) and action.get("/S") == "/GoTo":
                destination = action.get("/D")
                annotation.pop("/A", None)
        mapped = (
            _destination(destination, reader, destinations) if destination is not None else None
        )
        if destination is not None and mapped is None:
            # Do not leave links to pages omitted from the selected export.
            continue
        annotation = annotation.clone(writer, ignore_fields=("/P", "/StructParent", "/Parent"))
        rect = cast(ArrayObject, annotation["/Rect"])
        corners = [
            transform.apply_on((float(rect[x]), float(rect[y])))
            for x, y in ((0, 1), (0, 3), (2, 1), (2, 3))
        ]
        annotation[NameObject("/Rect")] = ArrayObject(
            [
                FloatObject(min(x for x, _ in corners)),
                FloatObject(min(y for _, y in corners)),
                FloatObject(max(x for x, _ in corners)),
                FloatObject(max(y for _, y in corners)),
            ]
        )
        if "/QuadPoints" in annotation:
            points = cast(ArrayObject, annotation["/QuadPoints"])
            annotation[NameObject("/QuadPoints")] = ArrayObject(
                [
                    FloatObject(value)
                    for index in range(0, len(points), 2)
                    for value in transform.apply_on(
                        (float(points[index]), float(points[index + 1]))
                    )
                ]
            )
        added = writer.add_annotation(spread, annotation)
        if mapped is not None:
            added[NameObject("/Dest")] = mapped
