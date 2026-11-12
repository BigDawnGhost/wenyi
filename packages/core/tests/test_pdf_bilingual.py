"""Bilingual PDF exports pair original and translated pages, not just filenames."""

import hashlib
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject
from wenyi_core.assemble.writer import assemble
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.pdf_bridge import BabeldocBridgeError
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.pdf_fixtures import pdf_bytes


@pytest.fixture(params=[FileStorage, SqliteStorage])
def pdf_project(tmp_path, monkeypatch, request):
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes(["Source one", "Source two"]))
    store = request.param(str(tmp_path / "state"))
    document = Document(
        title="Synthetic PDF",
        fmt="pdf",
        source_path=str(source),
        source_lang="en",
        target_lang="zh",
        meta={
            "babeldoc": True,
            "babeldoc_session_id": "offline-session",
            "babeldoc_bridge_url": "http://bridge.invalid",
        },
        chapters=[
            Chapter(
                index=0,
                segments=[
                    Segment(
                        index=0,
                        source="Source one",
                        target="Target one",
                        meta={"babeldoc_id": "0:0"},
                    )
                ],
            )
        ],
    )
    manifest = store.stage_document(
        document, source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    )
    store.save_manifest(manifest)
    response = {"content": pdf_bytes(["Target one", "Target two"])}
    calls = []

    def fillback(self, session_id, translations, *, out_path):
        calls.append((session_id, translations, out_path))
        Path(out_path).write_bytes(response["content"])
        return str(out_path)

    monkeypatch.setattr("wenyi_core.pdf_bridge.BabeldocBridgeClient.fillback", fillback)
    try:
        yield store, source, response, calls
    finally:
        store.close()


def _text_positions(page):
    positions = {}

    def locate(operator, operands, cm, tm):
        if operator == b"Tj":
            text = operands[0]
            label = text.decode("ascii") if isinstance(text, bytes) else str(text)
            positions[label] = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]

    page.extract_text(visitor_operand_before=locate)
    return positions


@pytest.mark.parametrize("order", [None, "target_first", "source_first"])
def test_pdf_bilingual_places_the_first_version_on_the_left(pdf_project, order):
    store, source, _, calls = pdf_project
    before = store.load_chapter(0).model_dump()

    options = {"order": order} if order is not None else {}
    output = assemble(store, str(source), bilingual=True, **options)

    assert Path(output).name == "source.zh-bi.pdf"
    reader = PdfReader(output)
    assert len(reader.pages) == 2
    for page, number in zip(reader.pages, ["one", "two"]):
        assert tuple(page.mediabox) == (0, 0, 400, 300)
        assert f"Source {number}" in page.extract_text()
        assert f"Target {number}" in page.extract_text()
        positions = _text_positions(page)
        source_x, target_x = (60, 260) if order == "source_first" else (260, 60)
        assert positions[f"Source {number}"] == pytest.approx(source_x)
        assert positions[f"Target {number}"] == pytest.approx(target_x)
    assert calls[0][:2] == ("offline-session", {"0:0": "Target one"})
    assert store.load_chapter(0).model_dump() == before


def test_pdf_monolingual_remains_the_unmodified_bridge_output(pdf_project):
    store, source, response, _ = pdf_project
    output = assemble(store, str(source))
    assert Path(output).read_bytes() == response["content"]


def _replace_source(store, source, content, *, pages=None):
    source.write_bytes(content)
    manifest = store.load_manifest()
    manifest["source_sha256"] = hashlib.sha256(content).hexdigest()
    manifest["meta"]["babeldoc_pages"] = pages
    store.save_manifest(manifest)


def test_pdf_pairs_the_saved_noncontiguous_page_selection(pdf_project):
    store, source, response, _ = pdf_project
    _replace_source(store, source, pdf_bytes([f"Source {i}" for i in range(1, 6)]), pages="2,4-5")
    response["content"] = pdf_bytes(["Target 2", "Target 4", "Target 5"])
    output = assemble(store, str(source), bilingual=True)
    pages = PdfReader(output).pages
    assert len(pages) == 3
    for page, index in zip(pages, [2, 4, 5], strict=True):
        assert f"Source {index}" in page.extract_text()
        assert f"Target {index}" in page.extract_text()


@pytest.mark.parametrize("order", ["target_first", "source_first"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_respects_visible_crop_rotation_units_and_unequal_sizes(pdf_project, rotation, order):
    store, source, response, _ = pdf_project
    original = pdf_bytes(["Original"], rotation=rotation, cropbox=(20, 30, 180, 250), user_unit=2)
    _replace_source(store, source, original)
    response["content"] = pdf_bytes(["Translated"], width=240, height=400)
    output = assemble(store, str(source), bilingual=True, order=order)
    page = PdfReader(output).pages[0]
    source_width, source_height = (320, 440) if rotation in (0, 180) else (440, 320)
    assert float(page.mediabox.width) == pytest.approx(source_width + 240)
    assert float(page.mediabox.height) == pytest.approx(max(source_height, 400))
    assert page.rotation == 0
    assert "Original" in page.extract_text()
    assert "Translated" in page.extract_text()
    positions = _text_positions(page)
    source_x = {0: 80, 90: 100, 180: 240, 270: 340}[rotation]
    if order == "target_first":
        source_x += 240
    target_x = source_width + 60 if order == "source_first" else 60
    assert positions["Original"] == pytest.approx(source_x)
    assert positions["Translated"] == pytest.approx(target_x)
    assert source.read_bytes() == original


@pytest.mark.parametrize("order", ["target_first", "source_first"])
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_transforms_link_rectangles_and_quads_with_visible_pages(pdf_project, rotation, order):
    from pypdf.annotations import Link
    from pypdf.generic import FloatObject, NameObject

    def linked_pdf(content):
        with PdfWriter() as writer:
            writer.append(PdfReader(BytesIO(content)))
            annotation = writer.add_annotation(
                0, Link(rect=(60, 75, 100, 90), url="https://example.org")
            )
            annotation[NameObject("/QuadPoints")] = ArrayObject(
                [FloatObject(value) for value in (60, 90, 100, 90, 60, 75, 100, 75)]
            )
            result = BytesIO()
            writer.write(result)
            return result.getvalue()

    store, source, response, _ = pdf_project
    original = linked_pdf(
        pdf_bytes(["Original"], rotation=rotation, cropbox=(20, 30, 180, 250), user_unit=2)
    )
    _replace_source(store, source, original)
    response["content"] = linked_pdf(pdf_bytes(["Translated"], width=240, height=400))
    output = PdfReader(assemble(store, str(source), bilingual=True, order=order))
    references = cast(ArrayObject, output.pages[0]["/Annots"])
    left, right = [cast(DictionaryObject, ref.get_object()) for ref in references]
    source_link, target_link = (left, right) if order == "source_first" else (right, left)
    expected = {
        0: (80, 90, 160, 120),
        90: (90, 240, 120, 320),
        180: (160, 320, 240, 350),
        270: (320, 160, 350, 240),
    }
    source_offset = 240 if order == "target_first" else 0
    source_rect = tuple(
        value + source_offset if index % 2 == 0 else value
        for index, value in enumerate(expected[rotation])
    )
    assert tuple(cast(ArrayObject, source_link["/Rect"])) == pytest.approx(source_rect)
    quads = cast(ArrayObject, source_link["/QuadPoints"])
    xs, ys = quads[::2], quads[1::2]
    assert (min(xs), min(ys), max(xs), max(ys)) == pytest.approx(source_rect)
    width, top_margin = (320, 40) if rotation in (0, 180) else (440, 0)
    target_offset = width if order == "source_first" else 0
    assert tuple(cast(ArrayObject, target_link["/Rect"])) == pytest.approx(
        (target_offset + 60, top_margin + 75, target_offset + 100, top_margin + 90)
    )
    assert source.read_bytes() == original


@pytest.mark.parametrize("content", [pdf_bytes(["Wrong page count"]), b"not a PDF", pdf_bytes([])])
def test_pdf_invalid_fillback_does_not_replace_existing_export(pdf_project, content):
    store, source, response, _ = pdf_project
    response["content"] = content
    output = source.parent / "edition.pdf"
    output.write_bytes(b"previous complete edition")
    with pytest.raises(BabeldocBridgeError):
        assemble(store, str(source), out_path=str(output), bilingual=True)
    assert output.read_bytes() == b"previous complete edition"
    assert list(source.parent.glob("edition.pdf.*.tmp")) == []


def test_pdf_source_mismatch_fails_before_bridge_request(pdf_project):
    store, source, _, calls = pdf_project
    source.write_bytes(pdf_bytes(["Different source"]))
    with pytest.raises(BabeldocBridgeError, match="source does not match"):
        assemble(store, str(source), bilingual=True)
    assert calls == []


@pytest.mark.parametrize("order", ["target_first", "source_first"])
@pytest.mark.parametrize("destination_kind", ["direct", "action", "named"])
def test_pdf_bilingual_links_target_spread_pages_on_the_correct_side(
    pdf_project, destination_kind, order
):
    from pypdf.annotations import Link
    from pypdf.generic import (
        Fit,
        FloatObject,
        NameObject,
        TextStringObject,
    )

    def linked_pdf(labels, destination):
        with PdfWriter() as writer:
            writer.append(PdfReader(BytesIO(pdf_bytes(labels))))
            annotation = writer.add_annotation(
                0, Link(rect=(60, 75, 100, 90), target_page_index=destination, fit=Fit.xyz(60, 90))
            )
            if destination_kind == "named":
                named = writer.add_named_destination("reference", destination)
                assert named is not None
                named_destination = cast(DictionaryObject, named.get_object())
                named_destination[NameObject("/D")] = ArrayObject(
                    [
                        writer.pages[destination].indirect_reference,
                        NameObject("/XYZ"),
                        FloatObject(60),
                        FloatObject(90),
                        FloatObject(0),
                    ]
                )
                annotation[NameObject("/Dest")] = TextStringObject("reference")
            elif destination_kind == "action":
                annotation.pop("/Dest")
                annotation[NameObject("/A")] = writer._add_object(
                    DictionaryObject(
                        {
                            NameObject("/S"): NameObject("/GoTo"),
                            NameObject("/D"): ArrayObject(
                                [
                                    writer.pages[destination].indirect_reference,
                                    NameObject("/XYZ"),
                                    FloatObject(60),
                                    FloatObject(90),
                                    FloatObject(0),
                                ]
                            ),
                        }
                    )
                )
            uri = writer.add_annotation(0, Link(rect=(100, 75, 140, 90), url="https://example.org"))
            uri[NameObject("/A")] = writer._add_object(uri["/A"])
            output = BytesIO()
            writer.write(output)
            return output.getvalue()

    store, source, response, _ = pdf_project
    _replace_source(store, source, linked_pdf(["Source 1", "Omitted", "Source 3"], 2), pages="1,3")
    response["content"] = linked_pdf(["Target 1", "Target 3"], 1)
    output = PdfReader(assemble(store, str(source), bilingual=True, order=order))
    references = cast(ArrayObject, output.pages[0]["/Annots"])
    links = [cast(DictionaryObject, ref.get_object()) for ref in references]
    internal = [link for link in links if "/Dest" in link]
    assert len(internal) == 2
    for link, expected_x in zip(internal, (60, 260), strict=True):
        destination = cast(ArrayObject, link["/Dest"])
        assert destination[0] == output.pages[1].indirect_reference
        assert float(destination[2]) == pytest.approx(expected_x)
        assert float(destination[3]) == pytest.approx(90)
        assert tuple(cast(ArrayObject, link["/Rect"])) == pytest.approx(
            (expected_x, 75, expected_x + 40, 90)
        )
    external = [link for link in links if link.get("/A") is not None]
    assert [cast(DictionaryObject, link["/A"])["/URI"] for link in external] == [
        "https://example.org"
    ] * 2
    assert [float(cast(ArrayObject, link["/Rect"])[0]) for link in external] == pytest.approx(
        (100, 300)
    )


def test_pdf_composition_failure_preserves_previous_output(pdf_project, monkeypatch):
    store, source, _, _ = pdf_project
    output = source.parent / "edition.pdf"
    output.write_bytes(b"previous complete edition")

    def fail(self, path):
        Path(path).write_bytes(b"partial")
        raise OSError("synthetic write failure")

    monkeypatch.setattr(PdfWriter, "write", fail)
    with pytest.raises(BabeldocBridgeError, match="write failure"):
        assemble(store, str(source), out_path=str(output), bilingual=True)
    assert output.read_bytes() == b"previous complete edition"
    assert list(source.parent.glob("edition.pdf.*.tmp")) == []
