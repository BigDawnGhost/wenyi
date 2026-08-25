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

from trans_novel.assemble.writer import _normalize_html_for_fpdf, assemble
from trans_novel.cli import _runstore_for
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.ingest.errors import MinerUError
from trans_novel.ingest.models import Document
from trans_novel.ingest.pdf_reader import pdf_cache_html_path
from trans_novel.ingest.segmenter import load_document
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore, source_sha256

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
            cached_html = pdf_cache_html_path(cache_dir, source_sha256(pdf_path))
            os.makedirs(os.path.dirname(cached_html))
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
        self.assertNotIn("pdf_path", document.meta)
        self.assertNotIn("converted_html_path", document.meta)
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

            with (
                patch(
                    "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                    side_effect=RuntimeError("connection reset"),
                ),
                self.assertRaisesRegex(MinerUError, "PDF 转换失败") as raised,
            ):
                load_document(
                    pdf_path,
                    "en",
                    "zh",
                    cache_dir=cache_dir,
                )

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_pdf_failed_conversion_cannot_leave_a_reusable_partial_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"invalid PDF; conversion is mocked")
            cache_dir = os.path.join(directory, "state", "sample", "source")

            def write_partial_then_fail(_input: str, output: str, **_kwargs) -> None:
                os.makedirs(os.path.dirname(output), exist_ok=True)
                with open(output, "w", encoding="utf-8") as file:
                    file.write("<html><body><p>PARTIAL</p></body></html>")
                raise RuntimeError("connection reset")

            with (
                patch(
                    "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                    side_effect=write_partial_then_fail,
                ),
                self.assertRaises(MinerUError),
            ):
                load_document(pdf_path, "en", "zh", cache_dir=cache_dir)

            def convert_fresh(_input: str, output: str, **_kwargs) -> None:
                os.makedirs(os.path.dirname(output), exist_ok=True)
                with open(output, "w", encoding="utf-8") as file:
                    file.write(_HTML.replace("First paragraph.", "Fresh retry."))

            with patch(
                "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                side_effect=convert_fresh,
            ) as conversion:
                document = load_document(pdf_path, "en", "zh", cache_dir=cache_dir)

            conversion.assert_called_once()
            self.assertIn("Fresh retry.", document.chapters[0].segments[1].source)

    def test_pdf_failed_conversion_metric_survives_successful_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"invalid PDF; conversion is mocked")
            state_dir = os.path.join(directory, "state")
            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": "zh"},
                    "llm": {"provider": "fake"},
                    "pipeline": {"book_understanding": False},
                    "paths": {"state_dir": state_dir},
                }
            )

            with patch("trans_novel.pipeline.runtime._RUN_METRICS_ENABLED", True):
                with (
                    patch(
                        "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                        side_effect=RuntimeError("temporary outage"),
                    ),
                    self.assertRaises(MinerUError),
                ):
                    Orchestrator(config, client=FakeClient()).prepare_for_translation(pdf_path)

                def convert_fresh(_input: str, output: str, **_kwargs) -> None:
                    os.makedirs(os.path.dirname(output), exist_ok=True)
                    with open(output, "w", encoding="utf-8") as file:
                        file.write(_HTML)

                with patch(
                    "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                    side_effect=convert_fresh,
                ):
                    store = Orchestrator(
                        config,
                        client=FakeClient(),
                    ).prepare_for_translation(pdf_path)

                self.assertEqual(
                    [metric["status"] for metric in store.load_run_metrics()],
                    ["failed", "completed"],
                )

    def test_orchestrator_uses_state_cache_and_resume_skips_pdf_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"not accessed when cached HTML exists")
            state_dir = os.path.join(directory, "state")
            cache_dir = os.path.join(state_dir, "sample", "source")
            cached_html = pdf_cache_html_path(cache_dir, source_sha256(pdf_path))
            os.makedirs(os.path.dirname(cached_html))
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
            serialized_manifest = str(store.load_manifest())

        self.assertEqual(store.run_dir, os.path.join(state_dir, "sample"))
        self.assertEqual(resumed.run_dir, store.run_dir)
        self.assertFalse(os.path.exists(cached_html))
        self.assertNotIn(os.path.abspath(pdf_path), serialized_manifest)
        self.assertNotIn(os.path.abspath(cached_html), serialized_manifest)

    def test_pdf_cache_isolated_by_source_hash_after_interrupted_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"old PDF")
            state_dir = os.path.join(directory, "state")
            cache_root = os.path.join(state_dir, "sample", "source")
            stale_hash = source_sha256(pdf_path)
            stale_html = pdf_cache_html_path(cache_root, stale_hash)
            os.makedirs(os.path.dirname(stale_html))
            with open(stale_html, "w", encoding="utf-8") as file:
                file.write(_HTML.replace("First paragraph.", "Stale body."))
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
            with (
                patch.object(RunStore, "save_manifest", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                Orchestrator(config, client=FakeClient()).prepare(pdf_path)

            partial_store = RunStore(os.path.join(state_dir, "sample"))
            stale_glossary = GlossaryStore(partial_store.glossary_path)
            stale_glossary.upsert_term(GlossaryTerm(source="OldBook", target="旧书"))
            stale_glossary.close()

            with open(pdf_path, "wb") as file:
                file.write(b"new PDF")
            fresh_hash = source_sha256(pdf_path)

            def convert(_input: str, output: str, **_kwargs) -> None:
                os.makedirs(os.path.dirname(output), exist_ok=True)
                with open(output, "w", encoding="utf-8") as file:
                    file.write(_HTML.replace("First paragraph.", "Fresh body."))

            with patch(
                "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                side_effect=convert,
            ) as conversion:
                store = Orchestrator(config, client=FakeClient()).prepare(pdf_path)

            conversion.assert_called_once()
            self.assertEqual(store.load_manifest()["source_sha256"], fresh_hash)
            self.assertIn("Fresh body.", store.load_chapter(0).segments[1].source)
            glossary = GlossaryStore(store.glossary_path)
            try:
                self.assertIsNone(glossary.get_term("OldBook"))
            finally:
                glossary.close()

    def test_pdf_change_during_conversion_is_rejected_and_cache_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"PDF before conversion")
            original_hash = source_sha256(pdf_path)
            state_dir = os.path.join(directory, "state")

            def convert(_input: str, output: str, **_kwargs) -> None:
                with open(pdf_path, "wb") as file:
                    file.write(b"PDF replaced during conversion")
                os.makedirs(os.path.dirname(output), exist_ok=True)
                with open(output, "w", encoding="utf-8") as file:
                    file.write(_HTML)

            config = Config.from_dict(
                {
                    "language": {"source": "en", "target": "zh"},
                    "llm": {"provider": "fake"},
                    "paths": {"state_dir": state_dir},
                }
            )
            with (
                patch(
                    "trans_novel.ingest.pdf_to_html.convert_pdf_to_html",
                    side_effect=convert,
                ),
                self.assertRaisesRegex(ValueError, "转换或解析期间发生变化"),
            ):
                Orchestrator(config, client=FakeClient()).prepare(pdf_path)

            stale_cache = os.path.dirname(
                pdf_cache_html_path(
                    os.path.join(state_dir, "sample", "source"),
                    original_hash,
                )
            )
            self.assertFalse(os.path.exists(stale_cache))
            self.assertFalse(os.path.isfile(os.path.join(state_dir, "sample", "manifest.json")))

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

    def test_pdf_generated_epub_packages_images_from_converted_html(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = os.path.join(directory, "sample.pdf")
            with open(pdf_path, "wb") as file:
                file.write(b"not accessed when cached HTML exists")
            cache_dir = os.path.join(directory, "state", "sample", "source")
            cached_html = pdf_cache_html_path(cache_dir, source_sha256(pdf_path))
            image_dir = os.path.join(os.path.dirname(cached_html), "images")
            os.makedirs(image_dir)
            with open(os.path.join(image_dir, "chart.svg"), "w", encoding="utf-8") as file:
                file.write(
                    '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
                )
            with open(cached_html, "w", encoding="utf-8") as file:
                file.write(
                    """<html><body><h1>Chapter</h1>
                    <p><img src="images/chart.svg"/>Before text.</p>
                    </body></html>"""
                )
            document = load_document(
                pdf_path,
                "en",
                "zh",
                cache_dir=cache_dir,
            )
            store = RunStore(os.path.join(directory, "state", "sample"))
            _initialize_test_store(store, document)
            _set_test_targets(store)
            output_path = os.path.join(directory, "translated.epub")

            assemble(
                store,
                pdf_path,
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
                self.assertIn(b"<svg", archive.read(asset_name))


class TestHtmlAndMarkdownIntegration(unittest.TestCase):
    def test_html_images_survive_translation_and_resources_are_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "images"))
            image_path = os.path.join(directory, "images", "chart.svg")
            with open(image_path, "w", encoding="utf-8") as file:
                file.write(
                    '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
                )
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
                package_name = next(name for name in names if name.endswith(".opf"))
                package = BeautifulSoup(archive.read(package_name), "xml")
                image = chapter.find("img")
                self.assertIsNotNone(image)
                assert image is not None
                src = image.get("src")
                self.assertIsInstance(src, str)
                assert isinstance(src, str)
                asset_name = next(name for name in names if name.endswith(src))
                self.assertTrue(archive.read(asset_name).startswith(b"GIF"))
                package_title = package.find("dc:title")
                self.assertIsNotNone(package_title)
                assert package_title is not None
                self.assertEqual(
                    package_title.get_text(),
                    "sample-wenyi-zh",
                )

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
                    "trans_novel.assemble.pdf_writer._find_fpdf_font",
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

    def test_fpdf_normalizer_preserves_named_anchor_metadata(self):
        normalized = _normalize_html_for_fpdf(
            """<html><body><p>
            <a class="decorative">plain</a>
            <a id="note-1" name="note-1">target</a>
            <a href="#note-1">jump</a>
            </p></body></html>""",
            base_dir=".",
        )
        rendered = BeautifulSoup(normalized, "html.parser")

        self.assertIsNone(rendered.find("a", class_="decorative"))
        destination = rendered.find("span", id="note-1")
        self.assertIsNotNone(destination)
        assert destination is not None
        self.assertEqual(destination.get("name"), "note-1")
        link = rendered.find("a", href="#note-1")
        self.assertIsNotNone(link)
        assert link is not None
        self.assertEqual(link.get_text(strip=True), "jump")

    def test_fpdf_normalizer_flattens_nested_tables_without_losing_rich_content(self):
        normalized = _normalize_html_for_fpdf(
            """<html><body><table><caption>Details</caption><tr>
            <th>Outer</th><td><img src="chart.png"/><a href="#note">caption</a>
            <table><tr><td><em>inner</em></td></tr></table></td>
            </tr></table></body></html>""",
            base_dir=".",
        )
        rendered = BeautifulSoup(normalized, "html.parser")

        self.assertIsNone(rendered.find("table"))
        self.assertIsNone(rendered.find("tr"))
        self.assertIsNone(rendered.find("td"))
        text = rendered.get_text(" ", strip=True)
        self.assertEqual(text.count("Details"), 1)
        self.assertEqual(text.count("Outer"), 1)
        self.assertEqual(text.count("inner"), 1)
        self.assertIsNotNone(rendered.find("em", string="inner"))
        image = rendered.find("img")
        self.assertIsNotNone(image)
        assert image is not None
        self.assertEqual(image.get("src"), os.path.abspath("chart.png"))
        self.assertEqual(image.get("width"), "340")
        self.assertIsNotNone(rendered.find("a", href="#note"))

    def test_fpdf_normalizer_preserves_image_only_table_cells(self):
        normalized = _normalize_html_for_fpdf(
            '<html><body><table><tr><td><img src="chart.png"/></td></tr></table></body></html>',
            base_dir=".",
        )
        rendered = BeautifulSoup(normalized, "html.parser")

        self.assertIsNone(rendered.find("table"))
        image = rendered.find("img")
        self.assertIsNotNone(image)
        assert image is not None
        self.assertEqual(image.get("src"), os.path.abspath("chart.png"))
        self.assertEqual(image.get("width"), "340")

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
