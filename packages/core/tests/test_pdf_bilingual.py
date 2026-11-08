"""Bilingual PDF exports pair original and translated pages, not just filenames."""

import hashlib
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
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


@pytest.mark.parametrize("order", ["target_first", "source_first"])
def test_pdf_bilingual_places_original_left_and_translation_right(pdf_project, order):
    store, source, _, calls = pdf_project
    before = store.load_chapter(0).model_dump()

    output = assemble(store, str(source), bilingual=True, order=order)

    assert Path(output).name == "source.zh-bi.pdf"
    reader = PdfReader(output)
    assert len(reader.pages) == 2
    for page, number in zip(reader.pages, ["one", "two"]):
        assert tuple(page.mediabox) == (0, 0, 400, 300)
        assert f"Source {number}" in page.extract_text()
        assert f"Target {number}" in page.extract_text()
        positions = {}

        def locate(operator, operands, cm, tm):
            if operator == b"Tj":
                text = operands[0]
                label = text.decode("ascii") if isinstance(text, bytes) else str(text)
                positions[label] = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]

        page.extract_text(visitor_operand_before=locate)
        assert positions[f"Source {number}"] == pytest.approx(60)
        assert positions[f"Target {number}"] == pytest.approx(260)
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


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_pdf_respects_visible_crop_rotation_units_and_unequal_sizes(pdf_project, rotation):
    store, source, response, _ = pdf_project
    original = pdf_bytes(["Original"], rotation=rotation, cropbox=(20, 30, 180, 250), user_unit=2)
    _replace_source(store, source, original)
    response["content"] = pdf_bytes(["Translated"], width=240, height=400)
    output = assemble(store, str(source), bilingual=True)
    page = PdfReader(output).pages[0]
    left_width, left_height = (320, 440) if rotation in (0, 180) else (440, 320)
    assert float(page.mediabox.width) == pytest.approx(left_width + 240)
    assert float(page.mediabox.height) == pytest.approx(max(left_height, 400))
    assert page.rotation == 0
    assert "Original" in page.extract_text()
    assert "Translated" in page.extract_text()
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
