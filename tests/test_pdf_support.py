"""PDF/HTML/Markdown ingestion and export integration tests."""

from __future__ import annotations

import os
import tempfile
import types
import unittest
import zipfile
from unittest.mock import patch

from bs4 import BeautifulSoup
from bs4.element import Comment

from trans_novel.assemble.writer import assemble
from trans_novel.cli import _runstore_for
from trans_novel.config import Config
from trans_novel.ingest.errors import MinerUError
from trans_novel.ingest.models import Document
from trans_novel.ingest.segmenter import load_document
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore


_HTML = """\
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Sample</title></head>
<body>
<h1>Chapter One</h1><p>First paragraph.</p>
<h2>Chapter Two</h2><p>Second paragraph.</p>
</body>
</html>
"""


def _set_test_targets(store: RunStore) -> None:
    manifest = store.load_manifest()
    for chapter_info in manifest["chapters"]:
        chapter = store.load_chapter(chapter_info["index"])
        for segment in chapter.segments:
            segment.target = f"译{chapter.index}-{segment.index}"
        store.save_chapter(chapter)


def _initialize_test_store(store: RunStore, document: Document) -> None:
    """Commit a parsed document using the current manifest-last store protocol."""
    manifest = store.stage_document(document)
    manifest["initialized"] = True
    store.save_manifest(manifest)


class TestPdfIngest(unittest.TestCase):
    def test_pdf_reuses_state_html_without_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"not accessed when cached HTML exists")
            cache_dir = os.path.join(directory, "state", "sample", "source")
            os.makedirs(cache_dir)
            cached_html = os.path.join(cache_dir, "converted.html")
            with open(cached_html, "w", encoding="utf-8") as file:
                file.write(_HTML)

            document = load_document(
                pdf_path,
                "en",
                "zh",
                cache_dir=cache_dir,
            )

        self.assertEqual(document.title, "sample")
        self.assertEqual(document.fmt, "pdf")
        self.assertEqual(document.source_path, os.path.abspath(pdf_path))
        self.assertEqual(
            document.meta["converted_html_path"],
            os.path.abspath(cached_html),
        )
        self.assertEqual(
            [chapter.title for chapter in document.chapters],
            ["Chapter One", "Chapter Two"],
        )
        self.assertTrue(all(chapter.template for chapter in document.chapters))

    def test_pdf_wraps_external_conversion_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"invalid PDF is not read because conversion is mocked")
            cache_dir = os.path.join(directory, "state", "sample", "source")

            with patch(
                "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                side_effect=RuntimeError("connection reset"),
            ):
                with self.assertRaisesRegex(MinerUError, "PDF 转换失败") as raised:
                    load_document(
                        pdf_path,
                        "en",
                        "zh",
                        cache_dir=cache_dir,
                    )

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_orchestrator_uses_state_cache_and_resume_skips_pdf_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"not accessed when cached HTML exists")
            state_dir = os.path.join(directory, "state")
            cache_dir = os.path.join(state_dir, "sample", "source")
            os.makedirs(cache_dir)
            cached_html = os.path.join(cache_dir, "converted.html")
            with open(cached_html, "w", encoding="utf-8") as file:
                file.write(_HTML)
            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": "zh"},
                    "llm": {
                        "provider": "fake",
                        "tiers": {"strong": {"model": "fake"}},
                    },
                    "paths": {"state_dir": state_dir},
                }
            )
            orchestrator = Orchestrator(config, client=FakeClient())

            store = orchestrator.prepare(pdf_path)
            os.remove(cached_html)
            resumed = orchestrator.prepare(pdf_path)

        self.assertEqual(store.run_dir, os.path.join(state_dir, "sample"))
        self.assertEqual(resumed.run_dir, store.run_dir)
        self.assertFalse(os.path.exists(cached_html))

    def test_cli_tools_locate_pdf_state_without_parsing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"PDF parsing must not run for status tools")
            state_dir = os.path.join(directory, "state")
            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": "zh"},
                    "llm": {
                        "provider": "fake",
                        "tiers": {"strong": {"model": "fake"}},
                    },
                    "paths": {"state_dir": state_dir},
                }
            )

            with patch(
                "trans_novel.cli.load_document",
                side_effect=AssertionError("PDF source should not be parsed"),
            ):
                store = _runstore_for(config, pdf_path)

        self.assertEqual(store.run_dir, os.path.join(state_dir, "sample"))


class TestHtmlAndMarkdownIntegration(unittest.TestCase):
    def test_html_images_survive_translation_and_resources_are_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "images"))
            image_path = os.path.join(directory, "images", "chart.svg")
            with open(image_path, "w", encoding="utf-8") as file:
                file.write('<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>')
            source_path = os.path.join(directory, "sample.html")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write(
                    """<html><body><h1>Chapter</h1>
                    <!-- image context must remain non-visible -->
                    <p><img src="images/chart.svg"/>Before text.</p>
                    <p>Middle <img src="images/chart.svg"/> text.</p>
                    <figure><picture><source srcset="images/chart.svg"/>
                    <img src="images/chart.svg"/></picture>
                    <figcaption>Visible caption.</figcaption></figure>
                    </body></html>"""
                )
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "output", "translated.html")

            assemble(store, source_path, out_path=output_path, out_format="html")
            with open(output_path, encoding="utf-8") as file:
                rendered = BeautifulSoup(file.read(), "html.parser")

            self.assertEqual(len(rendered.find_all("img")), 3)
            self.assertIsNotNone(rendered.find(string=lambda node: isinstance(node, Comment)))
            self.assertNotIn("image context must remain non-visible", rendered.get_text())
            self.assertIsNotNone(rendered.find("figcaption"))
            mixed = rendered.find("p", string=None)
            self.assertIsNotNone(mixed)
            for image in rendered.find_all("img"):
                src = image.get("src")
                self.assertIsInstance(src, str)
                assert isinstance(src, str)
                self.assertTrue(os.path.isfile(os.path.join(directory, "output", src)))

    def test_html_images_are_packaged_in_generated_epub(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "sample.html")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write(
                    """<html><body><h1>Chapter</h1>
                    <p>Before <img alt="dot"
                    src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="/>
                    after.</p></body></html>"""
                )
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.epub")

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="epub",
                about_page=False,
            )
            with zipfile.ZipFile(output_path) as archive:
                names = archive.namelist()
                chapter_name = next(name for name in names if name.endswith("/ch0.xhtml"))
                chapter = BeautifulSoup(archive.read(chapter_name), "html.parser")
                image = chapter.find("img")
                self.assertIsNotNone(image)
                assert image is not None
                src = image.get("src")
                self.assertIsInstance(src, str)
                assert isinstance(src, str)
                asset_name = next(name for name in names if name.endswith(src))
                self.assertTrue(archive.read(asset_name).startswith(b"GIF"))

    def test_pdf_export_uses_print_html_and_weasyprint(self):
        writes: list[tuple[str, str | None, str]] = []

        class FakeHTML:
            def __init__(self, *, string: str, base_url: str | None = None):
                self.string = string
                self.base_url = base_url

            def write_pdf(self, output: str) -> None:
                writes.append((self.string, self.base_url, output))
                with open(output, "wb") as file:
                    file.write(b"%PDF-fake")

        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "sample.html")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write(_HTML)
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.pdf")

            with patch.dict(
                "sys.modules",
                {"weasyprint": types.SimpleNamespace(HTML=FakeHTML)},
            ):
                result = assemble(
                    store,
                    source_path,
                    out_path=output_path,
                    out_format="pdf",
                )

            self.assertEqual(result, output_path)
            self.assertEqual(len(writes), 1)
            self.assertIn('id="trans-novel-print-style"', writes[0][0])
            self.assertTrue(os.path.isfile(output_path))

    def test_pdf_export_can_use_fpdf2_without_system_renderer(self):
        writes: list[dict[str, object]] = []

        class FakeFontFace:
            def __init__(self, **kwargs):
                self.options = kwargs

        class FakeFPDF:
            def __init__(self, **kwargs):
                self.options = kwargs

            def set_margins(self, *args):
                pass

            def set_auto_page_break(self, **kwargs):
                pass

            def add_font(self, *args, **kwargs):
                pass

            def alias_nb_pages(self):
                pass

            def add_page(self):
                pass

            def write_html(self, html, **kwargs):
                writes.append({"html": html, **kwargs})

            def output(self, path):
                with open(path, "wb") as file:
                    file.write(b"%PDF-fpdf2-fake")

        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "sample.html")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write(
                    """<html><body><h1>Chapter</h1>
                    <p>Before <img src="dot.png"/> after.</p>
                    </body></html>"""
                )
            with open(os.path.join(directory, "dot.png"), "wb") as file:
                file.write(b"not decoded by the mocked renderer")
            font_path = os.path.join(directory, "font.ttf")
            with open(font_path, "wb") as file:
                file.write(b"mock font")
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.pdf")

            with (
                patch.dict(
                    "sys.modules",
                    {
                        "fpdf": types.SimpleNamespace(
                            FPDF=FakeFPDF,
                            FontFace=FakeFontFace,
                        )
                    },
                ),
                patch(
                    "trans_novel.assemble.writer._find_fpdf_font",
                    return_value=font_path,
                ),
            ):
                result = assemble(
                    store,
                    source_path,
                    out_path=output_path,
                    out_format="pdf",
                    pdf_engine="fpdf2",
                )

        self.assertEqual(result, output_path)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["font_family"], "WenyiCJK")
        self.assertIn("<img", str(writes[0]["html"]))
        self.assertNotIn("<style", str(writes[0]["html"]))

    def test_html_export_has_one_head_and_translated_content(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "sample.html")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write(_HTML)
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "nested", "translated.html")

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="html",
            )
            with open(output_path, encoding="utf-8") as file:
                rendered = BeautifulSoup(file.read(), "html.parser")

        self.assertEqual(len(rendered.find_all("head")), 1)
        assert rendered.title is not None
        self.assertEqual(rendered.title.get_text(), "Sample")
        self.assertIn("译0-0", rendered.get_text())
        self.assertIsNone(rendered.select_one("[data-tn-id]"))

    def test_markdown_levels_survive_html_export(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "sample.md")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write("# One\n\nFirst.\n\n## Two\n\nSecond.\n")
            document = load_document(source_path, "en", "zh")
            self.assertEqual(
                [chapter.meta["heading_level"] for chapter in document.chapters],
                [1, 2],
            )
            store = RunStore(os.path.join(directory, "state"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.html")

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="html",
            )
            with open(output_path, encoding="utf-8") as file:
                rendered = BeautifulSoup(file.read(), "html.parser")

        assert rendered.h1 is not None
        assert rendered.h2 is not None
        self.assertEqual(rendered.h1.get_text(), "译0-0")
        self.assertEqual(rendered.h2.get_text(), "译1-0")
        self.assertIn("译0-1", rendered.get_text())

    def test_bilingual_html_includes_source_style(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "plain.md")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write("Original paragraph.\n")
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.html")

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="html",
                bilingual=True,
            )
            with open(output_path, encoding="utf-8") as file:
                rendered = BeautifulSoup(file.read(), "html.parser")

        self.assertIsNotNone(rendered.find("style", id="tn-bilingual-style"))
        self.assertIsNotNone(rendered.find("p", class_="tn-source"))

    def test_markdown_without_heading_uses_default_level(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "plain.md")
            with open(source_path, "w", encoding="utf-8") as file:
                file.write("A paragraph without a heading.\n")

            document = load_document(source_path, "en", "zh")

        self.assertEqual(len(document.chapters), 1)
        self.assertEqual(document.chapters[0].meta["heading_level"], 1)
        self.assertEqual(
            document.chapters[0].segments[0].source,
            "A paragraph without a heading.",
        )


if __name__ == "__main__":
    unittest.main()
