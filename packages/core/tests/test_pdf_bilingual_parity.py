"""Compare the native composer and compatibility writer using local synthetic PDFs."""

import shutil
import subprocess
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link, Text
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    Fit,
    NameObject,
    NullObject,
    NumberObject,
    RectangleObject,
)
from wenyi_core.assemble.pdf_writer import _epub_native, _write_spreads

from tests.pdf_fixtures import pdf_bytes, text_positions


def _bytes(writer):
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _native_and_legacy(tmp_path, source, target, indices, order):
    native_path = tmp_path / "native.pdf"
    legacy_path = tmp_path / "legacy.pdf"
    native_path.write_bytes(target)
    original = PdfReader(BytesIO(source))
    prepared = _epub_native.pdf_spread_source(source)
    assert not isinstance(prepared, str), prepared
    assert prepared.page_count == len(original.pages)
    assert (
        _epub_native.pdf_write_spreads(prepared, native_path, indices, order == "source_first")
        is None
    )
    _write_spreads(original, PdfReader(BytesIO(target)), indices, legacy_path, order=order)
    return PdfReader(native_path), PdfReader(legacy_path)


@pytest.mark.parametrize(
    ("order", "plain", "layout"),
    [
        ("source_first", "Source Translated", "Source" + " " * 36 + "Translated"),
        ("target_first", "Translated Source", "Translated" + " " * 32 + "Source"),
    ],
)
def test_pdf_text_extraction_matches_legacy_golden(tmp_path, order, plain, layout):
    native, legacy = _native_and_legacy(
        tmp_path, pdf_bytes(["Source"]), pdf_bytes(["Translated"]), [0], order
    )
    for page in (legacy.pages[0], native.pages[0]):
        assert page.extract_text() == plain
        assert page.extract_text(extraction_mode="layout") == layout


def test_pdf_preserves_to_unicode_and_distinguishes_resource_categories(tmp_path):
    def with_shared_names(font, pixels, unicode_map):
        with PdfWriter() as writer:
            writer.append(PdfReader(BytesIO(_graphics_pdf("unused", pixels, source=False))))
            page = writer.pages[0]
            resources = page["/Resources"]
            resources["/XObject"][NameObject("/F1")] = resources["/XObject"].pop("/Picture")
            resources["/Font"]["/F1"][NameObject("/BaseFont")] = NameObject(font)
            cmap = DecodedStreamObject()
            cmap.set_data(
                b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
                b"2 beginbfchar\n" + unicode_map + b"\nendbfchar\n"
            )
            resources["/Font"]["/F1"][NameObject("/ToUnicode")] = writer._add_object(cmap)
            stream = DecodedStreamObject()
            stream.set_data(
                b"% Leave /F1 Do and /F1 Tf alone in comments\n"
                b"/F1 BMC BT /F#31 12 Tf 60 80 Td [<41> 0 (B)] TJ ET EMC\n"
                b"q 5 0 0 5 60 100 cm /F#31 Do Q"
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
            return _bytes(writer)

    source = with_shared_names("/Courier", b"\x19\x80\xff", b"<41> <0048>\n<42> <0069>")
    target = with_shared_names("/Helvetica", b"\x40\xff\x32", b"<41> <8BD1>\n<42> <6587>")
    path = tmp_path / "spread.pdf"
    path.write_bytes(target)
    prepared = _epub_native.pdf_spread_source(source)
    assert not isinstance(prepared, str), prepared
    assert _epub_native.pdf_write_spreads(prepared, path, [0], True) is None
    page = PdfReader(path).pages[0]
    assert page.extract_text().split() == ["Hi", "译文"]
    assert page.extract_text(extraction_mode="layout").split() == ["Hi", "译文"]
    # Inspect the decoded stream itself, not pypdf's reserialization of its operations.
    assert page["/Contents"].get_data().count(b"% Leave /F1 Do and /F1 Tf alone in comments") == 2
    content = page.get_contents()
    assert content is not None
    fonts = [args[0] for args, operator in content.operations if operator == b"Tf"]
    images = [args[0] for args, operator in content.operations if operator == b"Do"]
    assert [args[0] for args, operator in content.operations if operator == b"BMC"] == ["/F1"] * 2
    assert [page["/Resources"]["/Font"][name]["/BaseFont"] for name in fonts] == [
        "/Courier",
        "/Helvetica",
    ]
    assert [page["/Resources"]["/XObject"][name].get_data() for name in images] == [
        b"\x19\x80\xff",
        b"\x40\xff\x32",
    ]


def _graphics_pdf(label, pixel, *, source, nested_form=False):
    crop = (-20, 30, 180, 250)
    with PdfWriter() as writer:
        options = dict(rotation=90, cropbox=crop, user_unit=1.25) if source else dict(height=320)
        writer.append(PdfReader(BytesIO(pdf_bytes([label], **options))))
        page = writer.pages[0]
        resources = page["/Resources"]
        if source:
            parent = page["/Parent"]
            for key in ("/Resources", "/CropBox", "/Rotate", "/MediaBox"):
                parent[NameObject(key)] = page.pop(key)
        else:
            resources["/Font"]["/F1"][NameObject("/BaseFont")] = NameObject("/Courier")

        image = DecodedStreamObject()
        image.set_data(pixel)
        image.update(
            {
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        resources[NameObject("/XObject")] = DictionaryObject(
            {NameObject("/Picture"): writer._add_object(image.flate_encode())}
        )
        # A clipped red shape would extend beyond the rotated half without a clip.
        operations = b"q 1 0 0 rg 65 235 18 75 re f Q\nq 9.123456789 0 0 10 60 100 cm /Picture Do Q"
        if nested_form:
            xobjects = resources["/XObject"]
            form = DecodedStreamObject()
            form.update(
                {
                    NameObject("/Subtype"): NameObject("/Form"),
                    NameObject("/BBox"): RectangleObject((0, 0, 1, 1)),
                    NameObject("/Resources"): DictionaryObject(
                        {NameObject("/XObject"): DictionaryObject(dict(xobjects))}
                    ),
                }
            )
            form.set_data(b"/Picture Do")
            xobjects[NameObject("/Stamp")] = writer._add_object(form)
            operations += b"\nq 10 0 0 10 80 100 cm /Stamp Do Q"
        drawing = DecodedStreamObject()
        drawing.set_data(operations)
        first_stream = page.raw_get("/Contents")
        second_stream = writer._add_object(drawing.flate_encode())
        page[NameObject("/Contents")] = writer._add_object(
            ArrayObject([first_stream, second_stream])
        )
        return _bytes(writer)


@pytest.fixture(params=["source_first", "target_first"])
def graphics_spreads(tmp_path, request):
    source = _graphics_pdf("Source", b"\x19\x80\xff", source=True, nested_form=True)
    target = _graphics_pdf("Translated", b"\x40\xff\x32", source=False, nested_form=True)
    return _native_and_legacy(tmp_path, source, target, [0], request.param)


def test_pdf_preserves_compound_content_and_inherited_attributes(graphics_spreads):
    native, legacy = graphics_spreads
    actual, expected = native.pages[0], legacy.pages[0]
    assert tuple(actual.mediabox) == pytest.approx(tuple(expected.mediabox))
    assert text_positions(actual) == pytest.approx(text_positions(expected))
    assert actual.extract_text() == expected.extract_text()
    assert actual.extract_text(extraction_mode="layout") == expected.extract_text(
        extraction_mode="layout"
    )
    content = actual.get_contents()
    assert content is not None
    assert content.get_data().count(b"9.123456789 0 0 10 60 100 cm") == 2
    resources = actual["/Resources"]
    assert {font.get_object()["/BaseFont"] for font in resources["/Font"].values()} == {
        "/Helvetica",
        "/Courier",
    }
    xobjects = [obj.get_object() for obj in resources["/XObject"].values()]
    expected_pixels = {b"\x19\x80\xff", b"\x40\xff\x32"}
    assert {image.get_data() for image in xobjects if image["/Subtype"] == "/Image"} == (
        expected_pixels
    )
    assert {
        form["/Resources"]["/XObject"]["/Picture"].get_data()
        for form in xobjects
        if form["/Subtype"] == "/Form"
    } == expected_pixels


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="requires Poppler pdftoppm")
def test_pdf_native_spread_renders_like_legacy(tmp_path, graphics_spreads):
    def render(name):
        ppm = subprocess.run(
            ["pdftoppm", "-r", "96", "-singlefile", str(tmp_path / name)],
            check=True,
            capture_output=True,
        ).stdout
        magic, size, depth, pixels = ppm.split(b"\n", 3)
        assert magic == b"P6" and depth == b"255"
        return size, pixels

    size, native = render("native.pdf")
    legacy_size, legacy = render("legacy.pdf")
    assert size == legacy_size and len(native) == len(legacy)
    for color in (b"\x19\x80\xff", b"\x40\xff\x32", b"\xff\x00\x00"):
        assert native.count(color) == legacy.count(color) > 0
    # Exact right-angle transforms differ from legacy's trig-rounded matrices.
    # Allow antialiasing at path edges but not moved or missing graphics.
    assert sum(abs(a - b) for a, b in zip(native, legacy, strict=True)) / len(native) < 0.75


@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_pdf_destinations_use_destination_page_geometry_and_skip_omitted_pages(tmp_path, order):
    with PdfWriter() as writer:
        writer.append(PdfReader(BytesIO(pdf_bytes(["Source", "Omitted"]))))
        rotated = PdfReader(
            BytesIO(
                pdf_bytes(["Destination"], rotation=90, cropbox=(10, 20, 160, 210), user_unit=2)
            )
        )
        writer.add_page(rotated.pages[0])
        fits = [
            Fit.xyz(left=None, top=95, zoom=1.25),
            Fit.fit_horizontally(top=50),
            Fit.fit_box_horizontally(),
            Fit.fit_vertically(left=25),
            Fit.fit_box_vertically(),
            Fit.fit_rectangle(left=20, bottom=30, right=75, top=200),
            Fit.fit_box(),
        ]
        for index, fit in enumerate(fits):
            x = 20 + index * 10
            writer.add_annotation(0, Link(rect=(x, 40, x + 8, 55), target_page_index=2, fit=fit))
        writer.add_annotation(0, Link(rect=(20, 10, 45, 20), target_page_index=1))
        source = _bytes(writer)

    native, legacy = _native_and_legacy(
        tmp_path, source, pdf_bytes(["First", "Second"], height=320), [0, 2], order
    )
    actual, expected = native.pages[0]["/Annots"], legacy.pages[0]["/Annots"]
    assert len(actual) == len(expected) == len(fits)
    for actual_ref, expected_ref in zip(actual, expected, strict=True):
        ann, old_ann = actual_ref.get_object(), expected_ref.get_object()
        assert ann.raw_get("/P") == native.pages[0].indirect_reference
        dest, old_dest = ann["/Dest"], old_ann["/Dest"]
        assert dest[0] == native.pages[1].indirect_reference
        assert old_dest[0] == legacy.pages[1].indirect_reference
        assert dest[1] == old_dest[1]
        coordinates = [None if isinstance(value, NullObject) else value for value in dest[2:]]
        old_coordinates = [
            None if isinstance(value, NullObject) else value for value in old_dest[2:]
        ]
        assert coordinates == pytest.approx(old_coordinates)
        assert ann["/Rect"] == pytest.approx(old_ann["/Rect"])


def test_pdf_non_link_annotations_fall_back_without_overwriting_fillback(tmp_path):
    source = pdf_bytes(["Original first", "Original second"])
    with PdfWriter() as writer:
        writer.append(PdfReader(BytesIO(pdf_bytes(["Target first", "Target second"]))))
        writer.add_annotation(1, Text(rect=(70, 80, 100, 105), text="Keep this note"))
        page = writer.pages[1]
        page[NameObject("/Annots")] = writer._add_object(page["/Annots"])
        target = _bytes(writer)
    path = tmp_path / "temp.pdf"
    path.write_bytes(target)

    prepared = _epub_native.pdf_spread_source(source)
    assert not isinstance(prepared, str), prepared
    fallback = _epub_native.pdf_write_spreads(prepared, path, [0, 1], True)

    assert fallback is not None and "non-link" in fallback
    assert path.read_bytes() == target
    _write_spreads(PdfReader(BytesIO(source)), PdfReader(path), [0, 1], path, order="source_first")
    page = PdfReader(path).pages[1]
    note = page["/Annots"][0].get_object()
    assert note["/Contents"] == "Keep this note"
    assert tuple(note["/Rect"]) == (270, 80, 300, 105)
    assert " ".join(page.extract_text(extraction_mode="layout").split()) == (
        "Original second Target second"
    )


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (b"q BI /W 1 /H 1 /BPC 8 /CS /RGB ID \x19\x80\xff EI Q", "inline PDF image"),
        (b"Q", "unbalanced PDF graphics state"),
        (b"BX /F1 FutureOperator EX", "unsupported PDF content operator"),
    ],
)
def test_pdf_declines_content_it_cannot_safely_flatten(tmp_path, extra, reason):
    source = pdf_bytes(["Source"])
    with PdfWriter() as writer:
        writer.append(PdfReader(BytesIO(pdf_bytes(["Translated"]))))
        page = writer.pages[0]
        stream = page["/Contents"]
        stream.set_data(stream.get_data() + extra + b"\n")
        target = _bytes(writer)
    path = tmp_path / "fillback.pdf"
    path.write_bytes(target)
    prepared = _epub_native.pdf_spread_source(source)
    assert not isinstance(prepared, str), prepared
    fallback = _epub_native.pdf_write_spreads(prepared, path, [0], True)
    assert fallback is not None and reason in fallback
    assert path.read_bytes() == target
