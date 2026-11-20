"""Parity for native BabelDOC preflight and chapter extraction, with no bridge service."""

from unittest.mock import Mock

import pypdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)
from wenyi_core.ingest import pdf_babeldoc as ingest
from wenyi_core.markup import _epub_native
from wenyi_core.pdf_bridge import BabeldocBridgeError


def pdf_dict(**items):
    return DictionaryObject({NameObject(f"/{key}"): value for key, value in items.items()})


def add_stream(writer, data, *, compressed=False, **items):
    stream = DecodedStreamObject()
    stream.update(pdf_dict(**items))
    stream.set_data(data)
    return writer._add_object(stream.flate_encode() if compressed else stream)


def add_image(writer):
    return add_stream(
        writer,
        b"\x00",
        Type=NameObject("/XObject"),
        Subtype=NameObject("/Image"),
        ColorSpace=NameObject("/DeviceGray"),
        Width=NumberObject(1),
        Height=NumberObject(1),
        BitsPerComponent=NumberObject(8),
    )


def add_form(writer, content, **resources):
    return add_stream(
        writer,
        content,
        Type=NameObject("/XObject"),
        Subtype=NameObject("/Form"),
        BBox=RectangleObject((0, 0, 100, 100)),
        Resources=pdf_dict(**resources),
    )


def simple_font(encoding=None):
    font = pdf_dict(
        Type=NameObject("/Font"), Subtype=NameObject("/Type1"), BaseFont=NameObject("/Helvetica")
    )
    if encoding is not None:
        font[NameObject("/Encoding")] = encoding
    return pdf_dict(F=font)


def add_page(writer, content=None, **resources):
    page = writer.add_blank_page(width=100, height=100)
    if content is not None:
        page[NameObject("/Contents")] = content
    page[NameObject("/Resources")] = pdf_dict(**resources)
    return page


def write_pdf(writer, tmp_path):
    path = tmp_path / "input.pdf"
    writer.write(path)
    return path


@pytest.fixture
def mixed_pdf(tmp_path):
    writer = PdfWriter()
    add_page(writer)
    image = add_image(writer)
    inner = add_form(writer, b"/I Do", XObject=pdf_dict(I=image))
    outer = add_form(writer, b"/Inner Do", XObject=pdf_dict(Inner=inner))
    # Even unused resource images count, including images nested inside forms.
    add_page(writer, XObject=pdf_dict(Outer=outer))
    add_page(
        writer,
        ArrayObject(
            [
                add_stream(writer, b"/I Do\nBT /F 10 Tf 3 Tr", compressed=True),
                add_stream(writer, b"[(OCR) 5 ( layer)] TJ ET"),
            ]
        ),
        Font=simple_font(),
        XObject=pdf_dict(I=image),
    )
    whitespace = add_form(writer, b"BT /F 10 Tf (  ) Tj ET", Font=simple_font())
    add_page(writer, add_stream(writer, b"/F Do /I Do"), XObject=pdf_dict(F=whitespace, I=image))
    form_text = add_form(writer, b"BT /F 10 Tf (Form text) ' ET", Font=simple_font())
    add_page(writer, add_stream(writer, b"/F Do /I Do"), XObject=pdf_dict(F=form_text, I=image))
    return write_pdf(writer, tmp_path)


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        (None, False),
        ("", False),
        ("1", False),
        ("2", True),
        (" 2, 1-2, 2 ", True),
        ("-2", True),
        ("4", True),
        ("4-", False),
        ("5", False),
        ("١-٢", True),
        ("7-,2", False),
        ("2,invalid", False),
        ("2,99", True),
        ("99", False),
        ("2,-", False),
    ],
)
def test_native_preflight_matches_pypdf_on_selected_pages(mixed_pdf, monkeypatch, pages, expected):
    native = _epub_native.PdfInspection(mixed_pdf)
    indices = ingest._selected_page_indices(pages, native.page_count)
    assert native.image_only(indices) is expected
    assert ingest._is_image_only_pdf(mixed_pdf, pages=pages) is expected
    monkeypatch.setattr(ingest, "_open_native_pdf", lambda _: None)
    assert ingest._is_image_only_pdf(mixed_pdf, pages=pages) is expected


def test_scan_detection_precedes_bridge_request(mixed_pdf, monkeypatch):
    client = Mock()
    monkeypatch.setattr(ingest, "BabeldocBridgeClient", client)
    with pytest.raises(BabeldocBridgeError, match="only scanned images.*MinerU"):
        ingest.read_pdf_babeldoc(
            str(mixed_pdf), "en", "zh", bridge_url="http://bridge.test", pages="2"
        )
    client.assert_not_called()


@pytest.mark.parametrize(
    ("data", "glyph", "expected"),
    [
        (b"/I Do BT /F 10 Tf (x) Tj ET", (120, "/space"), True),
        (b"/I Do BT /F 10 Tf ( ) Tj ET", (32, "/X"), False),
        (b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x01 EI", None, True),
        (b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x01 EI BT /F 10 Tf (OCR) Tj ET", None, False),
    ],
)
def test_ambiguous_text_and_inline_images_use_pypdf(tmp_path, monkeypatch, data, glyph, expected):
    writer = PdfWriter()
    resources = {}
    if glyph is not None:
        code, name = glyph
        resources["Font"] = simple_font(
            pdf_dict(Differences=ArrayObject([NumberObject(code), NameObject(name)]))
        )
        resources["XObject"] = pdf_dict(I=add_image(writer))
    elif b"(OCR)" in data:
        resources["Font"] = simple_font()
    add_page(writer, add_stream(writer, data), **resources)
    pdf = write_pdf(writer, tmp_path)
    assert _epub_native.PdfInspection(pdf).image_only([0]) is None
    reader = Mock(wraps=PdfReader)
    monkeypatch.setattr(pypdf, "PdfReader", reader)
    assert ingest._is_image_only_pdf(pdf) is expected
    reader.assert_called_once_with(str(pdf))


def test_native_uses_inherited_images(tmp_path, monkeypatch):
    writer = PdfWriter()
    page = add_page(writer, add_stream(writer, b"/I Do"))
    page.pop("/Resources")
    writer.get_object(writer._pages)[NameObject("/Resources")] = pdf_dict(
        XObject=pdf_dict(I=add_image(writer))
    )
    pdf = write_pdf(writer, tmp_path)
    assert _epub_native.PdfInspection(pdf).image_only([0]) is True
    monkeypatch.setattr(ingest, "_open_native_pdf", lambda _: None)
    assert ingest._is_image_only_pdf(pdf) is True


def test_native_outline_matches_pypdf(tmp_path, monkeypatch):
    writer = PdfWriter()
    for _ in range(6):
        add_page(writer)
    part = writer.add_outline_item("Part", 0)
    chapter = writer.add_outline_item("First Chapter", 0, parent=part)
    writer.add_outline_item("Too deep", 1, parent=chapter)
    writer.add_outline_item("  最初の章  ", 0, parent=part, color=(0, 0, 1), italic=True)
    writer.add_outline_item("Old", 3)
    last = writer.add_outline_item("New", 3).get_object()
    # Explicit, without a GoTo action.
    last[NameObject("/Dest")] = last.pop("/A").get_object()["/D"]
    heading = writer.add_outline_item("No destination", None)
    writer.add_outline_item("Child of heading", 4, parent=heading)
    writer.add_outline_item("   ", 5)
    pdf = write_pdf(writer, tmp_path)
    expected = [(0, "最初の章"), (3, "New"), (4, "Child of heading")]
    assert _epub_native.PdfInspection(pdf).outline() is not None
    assert ingest.toc_chapter_starts(pdf) == expected
    monkeypatch.setattr(ingest, "_open_native_pdf", lambda _: None)
    assert ingest.toc_chapter_starts(pdf) == expected


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("named", [(1, "Title")]), ("wrapped dest", [(1, "Title")]), ("le", [(1, "本")])],
)
def test_outline_uses_pypdf_for_unusual_bookmarks(tmp_path, monkeypatch, kind, expected):
    writer = PdfWriter()
    for _ in range(2):
        add_page(writer)
    item = writer.add_outline_item("Title", 1).get_object()
    action = item["/A"]
    if kind == "named":
        writer.add_named_destination("chapter", 1)
        action[NameObject("/D")] = TextStringObject("chapter")
    elif kind == "wrapped dest":
        del item["/A"]
        item[NameObject("/Dest")] = pdf_dict(D=action["/D"])
    else:
        item[NameObject("/Title")] = ByteStringObject(b"\xff\xfe" + "本".encode("utf-16-le"))
    pdf = write_pdf(writer, tmp_path)
    assert _epub_native.PdfInspection(pdf).outline() is None
    reader = Mock(wraps=PdfReader)
    monkeypatch.setattr(pypdf, "PdfReader", reader)
    assert ingest.toc_chapter_starts(pdf) == expected
    reader.assert_called_once_with(str(pdf))


@pytest.mark.parametrize("compatibility", [False, True])
def test_ingest_reuses_pdf_inspection(tmp_path, monkeypatch, compatibility):
    writer = PdfWriter()
    page = add_page(
        writer,
        add_stream(
            writer,
            (b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x01 EI\n" if compatibility else b"")
            + b"BT /F 10 Tf (text) Tj ET",
        ),
        Font=simple_font(),
    )
    writer.add_outline_item("Title", page)
    pdf = write_pdf(writer, tmp_path)
    open_native = Mock(wraps=_epub_native.PdfInspection)
    monkeypatch.setattr(_epub_native, "PdfInspection", open_native)
    pypdf_reader = Mock(wraps=PdfReader)
    monkeypatch.setattr(pypdf, "PdfReader", pypdf_reader)
    bridge = Mock()
    bridge.return_value.extract.return_value = {
        "session_id": "session",
        "paragraphs": {"paragraphs": [{"id": "0:2", "page": 0, "source": "text"}]},
    }
    monkeypatch.setattr(ingest, "BabeldocBridgeClient", bridge)
    doc = ingest.read_pdf_babeldoc(str(pdf), "en", "zh", bridge_url="http://bridge.test", pages="1")
    open_native.assert_called_once_with(str(pdf))
    if compatibility:
        pypdf_reader.assert_called_once_with(str(pdf))
    else:
        pypdf_reader.assert_not_called()
    bridge.return_value.extract.assert_called_once_with(str(pdf), pages="1")
    assert doc.meta["pdf_toc_chapters"] == 1
    assert doc.chapters[0].title == "Title"
    assert doc.chapters[0].segments[0].meta["babeldoc_id"] == "0:2"


def test_unreadable_pdf_is_left_to_bridge_and_not_reopened_for_outlines(tmp_path, monkeypatch):
    pdf = tmp_path / "unreadable.pdf"
    pdf.write_bytes(b"not a PDF")
    reader = Mock(wraps=PdfReader)
    monkeypatch.setattr(pypdf, "PdfReader", reader)
    bridge = Mock()
    bridge.return_value.extract.return_value = {
        "session_id": "session",
        "paragraphs": {"paragraphs": [{"id": "0:0", "source": "Recovered text"}]},
    }
    monkeypatch.setattr(ingest, "BabeldocBridgeClient", bridge)
    doc = ingest.read_pdf_babeldoc(str(pdf), "en", "zh", bridge_url="http://bridge.test")
    reader.assert_called_once_with(str(pdf))
    bridge.return_value.extract.assert_called_once_with(str(pdf), pages=None)
    assert doc.meta["pdf_toc_chapters"] == 0
    assert doc.chapters[0].title == "unreadable"


def test_chapter_assignment_keeps_out_of_order_paragraphs_and_empty_chapter_semantics():
    paragraphs = [
        {"id": "50:after", "source": "Last chapter"},
        {"id": "missing", "source": "No page"},
        {"id": "50:before", "page": 0, "source": "Before first start"},
        {"id": "12:exact", "page": -1, "source": "Second chapter start"},
        {"id": "3:later", "page": 51, "source": "Still last"},
    ]
    chapters = ingest._chapters_from_paragraphs(
        paragraphs,
        book_title="Book",
        toc_starts=[(2, "First"), (12, "Second"), (15, "Empty"), (50, "Last")],
    )
    assert [
        (chapter.index, chapter.title, chapter.meta["pdf_toc_page"]) for chapter in chapters
    ] == [
        (0, "First", 2),
        (1, "Second", 12),
        (2, "Last", 50),
    ]
    assert [
        [(segment.index, segment.meta["babeldoc_id"]) for segment in chapter.segments]
        for chapter in chapters
    ] == [
        [(0, "missing"), (1, "50:before")],
        [(0, "12:exact")],
        [(0, "50:after"), (1, "3:later")],
    ]
