"""Optional real-font PDF regressions; CI installs fpdf2, CJK fonts and Poppler."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from wenyi_core.assemble.pdf_writer import _assemble_pdf_fpdf2, _find_fpdf_font
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.pipeline.runstore import ExportSnapshotStore


def _font_path() -> Path:
    candidates = [
        os.environ.get("WENYI_TEST_TTF_FONT", ""),
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    pytest.skip("Install fonts-wqy-zenhei or set WENYI_TEST_TTF_FONT for real CJK PDF coverage")


def _book(tmp_path):
    source = tmp_path / "story.txt"
    source.write_text("A short story.", encoding="utf-8")
    chapter = Chapter(
        index=0, segments=[Segment(index=0, source="A short story.", target="一篇简短的故事。")]
    )
    store = ExportSnapshotStore(
        str(tmp_path / "resources"),
        {
            "title": "PDF rendering test",
            "fmt": "text",
            "source_lang": "en",
            "target_lang": "zh",
            "chapters": [{"index": 0, "title": "", "status": "done"}],
        },
        {0: chapter},
    )
    return source, store


def test_real_cjk_truetype_embeds_and_renders_with_poppler(tmp_path, monkeypatch):
    pytest.importorskip("fpdf")
    pytest.importorskip("fontTools")
    from PIL import Image  # type: ignore[import-not-found]
    from pypdf import PdfReader

    font = _font_path()
    renderer = shutil.which("pdftoppm")
    extractor = shutil.which("pdftotext")
    if not renderer or not extractor:
        pytest.skip("Install Poppler to verify actual glyph rendering")
    monkeypatch.setenv("TRANS_NOVEL_PDF_FONT", str(font))
    source, store = _book(tmp_path)
    output = tmp_path / "story.pdf"
    _assemble_pdf_fpdf2(store, str(source), str(output), bilingual=True)
    reader = PdfReader(output)
    fonts = reader.pages[0]["/Resources"]["/Font"].get_object()  # type: ignore[index]
    for reference in fonts.values():
        descendant = reference.get_object()["/DescendantFonts"][0].get_object()
        assert descendant["/Subtype"] == "/CIDFontType2"
        stream = descendant["/FontDescriptor"]["/FontFile2"].get_object().get_data()
        assert stream[:4] == b"\x00\x01\x00\x00"  # TrueType, not mislabeled OpenType/CFF.
    text = subprocess.run([extractor, str(output), "-"], check=True, capture_output=True, text=True)
    assert not text.stderr.strip()
    assert "一篇简短的故事。" in text.stdout
    assert "A short story." in text.stdout
    assert "1/1" in text.stdout
    rendered = subprocess.run(
        [
            renderer,
            "-f",
            "1",
            "-singlefile",
            "-r",
            "100",
            "-png",
            str(output),
            str(tmp_path / "page"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not rendered.stderr.strip()
    with Image.open(tmp_path / "page.png") as page:
        # The broken CFF embedding extracted correctly but rendered only one Chinese glyph.
        # Both actual text rows must occupy visible ink across a meaningful line width.
        body = page.convert("L").crop((50, 60, 250, 125))
        occupied_columns = sum(
            any(body.getpixel((x, y)) < 180 for y in range(body.height)) for x in range(body.width)
        )
        assert occupied_columns > 70


def test_explicit_cff_font_is_rejected_before_corrupt_pdf_is_created(tmp_path, monkeypatch):
    pytest.importorskip("fpdf")
    font_tools = pytest.importorskip("fontTools.ttLib")
    font = Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc")
    if not font.is_file():
        pytest.skip("Install fonts-noto-cjk for the real CFF rejection regression")
    with font_tools.TTFont(font, fontNumber=0, lazy=True) as loaded:
        assert "CFF " in loaded and "glyf" not in loaded
    monkeypatch.setenv("TRANS_NOVEL_PDF_FONT", str(font))
    source, store = _book(tmp_path)
    output = tmp_path / "rejected.pdf"
    with pytest.raises(RuntimeError, match="TrueType outlines"):
        _assemble_pdf_fpdf2(store, str(source), str(output), bilingual=True)
    assert not output.exists()


def test_automatic_font_selection_skips_cff_collection(monkeypatch):
    pytest.importorskip("fpdf")
    pytest.importorskip("fontTools")
    compatible = _font_path()
    cff = Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc")
    if not cff.is_file():
        pytest.skip("Install fonts-noto-cjk for automatic CFF fallback coverage")
    original = os.path.isfile
    monkeypatch.delenv("TRANS_NOVEL_PDF_FONT", raising=False)

    def available(path):
        if path == "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc":
            return True
        return path == str(cff) and original(path)

    monkeypatch.setattr(os.path, "isfile", available)
    # A test font can live outside system directories, as in an unprivileged developer setup.
    from fontTools import ttLib  # type: ignore[import-not-found]

    real_ttfont = ttLib.TTFont
    monkeypatch.setattr(
        ttLib,
        "TTFont",
        lambda path, **kwargs: real_ttfont(
            str(compatible) if path == "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc" else path,
            **kwargs,
        ),
    )
    assert _find_fpdf_font() == "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
