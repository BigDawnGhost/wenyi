"""MinerU uploads use ordered page subsets and never expose unfinished chunks."""

from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject
from wenyi_core.ingest import mineru_api

from tests.pdf_fixtures import pdf_bytes


@pytest.mark.parametrize("fallback", [False, True])
def test_chunks_preserve_page_content_boxes_units_and_order(tmp_path, monkeypatch, fallback):
    source = tmp_path / "input.pdf"
    source.write_bytes(
        pdf_bytes(["one", "two", "three"], rotation=90, cropbox=(10, 20, 150, 240), user_unit=2)
    )
    if fallback:
        # Exercise a real native decline on the last page, after the first chunk exists.
        with PdfReader(source) as reader, PdfWriter() as writer:
            writer.append_pages_from_reader(reader)
            writer.pages[-1][NameObject("/Marker")] = TextStringObject("keep me")
            writer.write(tmp_path / "marked.pdf")
        source = tmp_path / "marked.pdf"
    else:
        monkeypatch.setattr(mineru_api, "PdfReader", lambda *_: pytest.fail("unexpected fallback"))
    chunks = tmp_path / "chunks"
    chunks.mkdir()
    original = source.read_bytes()
    count, paths = mineru_api._prepare_chunks(source, chunks, 2)
    assert count == 3
    assert [path.name for path in paths] == ["chunk_p1-2.pdf", "chunk_p3-3.pdf"]
    labels = []
    for path in paths:
        with PdfReader(path) as reader:
            for page in reader.pages:
                labels.append(page.extract_text().strip())
                assert page.rotation == 90 and page.user_unit == 2
                assert tuple(page.cropbox) == (10, 20, 150, 240)
                if fallback and labels[-1] == "three":
                    assert page["/Marker"] == "keep me"
    assert labels == ["one", "two", "three"]
    assert source.read_bytes() == original


def test_small_pdf_uploads_the_exact_original(tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf_bytes(["one", "two"]))
    assert mineru_api._prepare_chunks(source, tmp_path, 2) == (2, [source])
    assert list(tmp_path.iterdir()) == [source]


def test_failed_upload_removes_partial_chunks_only(tmp_path, monkeypatch):
    source, out = tmp_path / "input.pdf", tmp_path / "existing.html"
    source.write_bytes(pdf_bytes(["one", "two"]))
    out.write_text("keep", encoding="utf-8")
    seen = []

    def fail_upload(self, names):
        seen.extend(map(Path, names))
        assert all(path.exists() for path in seen)
        raise OSError("upload interrupted")

    monkeypatch.setattr(mineru_api, "MAX_PAGES", 1)
    monkeypatch.setattr(mineru_api.MinerUApi, "submit_batch", fail_upload)
    with pytest.raises(OSError, match="interrupted"):
        mineru_api.extract_pdf(str(source), api_token="synthetic")
    assert len(seen) == 2 and all(not path.parent.exists() for path in seen)
    assert out.read_text(encoding="utf-8") == "keep" and source.exists()
