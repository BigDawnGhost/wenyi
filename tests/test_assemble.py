"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from bs4 import BeautifulSoup
from bs4.element import Tag

from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore
from trans_novel.assemble.writer import (
    _inject_bilingual_style,
    _render_chapter_html,
    _rewrite_html_document,
    _rewrite_opf_metadata,
    assemble,
)
from trans_novel.assemble.about import about_xhtml, append_about_page
from trans_novel.assemble.report import build_report
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.segmenter import load_document
from trans_novel.ingest.epub_reader import annotate_epub_resource
from trans_novel.ingest.models import Chapter
from tests.sample_data import (
    write_inline_sample_epub,
    write_nested_toc_epub,
    write_sample_epub,
    write_sample_txt,
)
from tests.fake_llm import routing_handler


_FB2_WITH_IMAGES = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
             xmlns:xlink="http://www.w3.org/1999/xlink">
<description><title-info>
  <book-title>Illustrated Book</book-title>
  <coverpage><image xlink:href="#cover.jpg"/></coverpage>
</title-info></description>
<body><section><title><p>Chapter</p></title>
  <image xlink:href="#inside.png"/><p>Illustrated text.</p>
</section></body>
<binary id="cover.jpg" content-type="image/jpeg">Y292ZXItYnl0ZXM=</binary>
<binary id="inside.png" content-type="image/png">aW5zaWRlLWJ5dGVz</binary>
</FictionBook>
"""


def _write_vertical_epub(path: str) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>縦書き小説</dc:title>
    <dc:language>ja</dc:language>
  </metadata>
  <manifest>
    <item id="style" href="style.css" media-type="text/css"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine page-progression-direction="rtl">
    <itemref idref="ch1"/>
  </spine>
</package>
"""
    ch1 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" class="vrtl"><head>
<title>第一章</title><link rel="stylesheet" href="style.css"/>
</head><body>
<h1>第一章　出会い</h1>
<p>綾小路は教室の窓際に座っていた。</p>
</body></html>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/style.css", "html { writing-mode: vertical-rl; }")
        zf.writestr("OEBPS/ch1.xhtml", ch1)


def _config(state_dir: str):
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
        "pipeline": {"review": True, "polish": True, "backtranslate_sample": 0.0},
        "paths": {"state_dir": state_dir},
    })


def _run(input_path, state_dir):
    cfg = _config(state_dir)
    orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
    return orch.run(input_path), cfg


class TestAssembleText(unittest.TestCase):
    def test_fb2_images_and_cover_are_preserved_in_generated_epub(self):
        with tempfile.TemporaryDirectory() as d:
            fb2 = os.path.join(d, "illustrated.fb2")
            with open(fb2, "w", encoding="utf-8") as file:
                file.write(_FB2_WITH_IMAGES)
            store, _ = _run(fb2, os.path.join(d, "state"))

            out = assemble(store, fb2, out_format="epub", about_page=False)

            with zipfile.ZipFile(out) as archive:
                names = archive.namelist()
                cover_name = next(name for name in names if name.endswith("images/cover.jpg"))
                inside_name = next(name for name in names if name.endswith("images/inside.png"))
                chapter_name = next(name for name in names if name.endswith("/ch0.xhtml"))
                chapter = BeautifulSoup(archive.read(chapter_name), "html.parser")
                package_name = next(name for name in names if name.endswith("content.opf"))
                package = BeautifulSoup(archive.read(package_name), "xml")

                self.assertEqual(archive.read(cover_name), b"cover-bytes")
                self.assertEqual(archive.read(inside_name), b"inside-bytes")

        image = chapter.find("img", src="images/inside.png")
        self.assertIsNotNone(image)
        cover_item = package.find("item", properties="cover-image")
        self.assertIsNotNone(cover_item)

    def test_txt_input_to_txt(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="txt")
            self.assertTrue(out.endswith(".txt"))
            self.assertEqual(os.path.basename(out), "novel.zh.txt")
            self.assertEqual(os.path.dirname(out), os.path.join(d, "output"))
            with open(out, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("润0", content)  # 译文已写入

    def test_about_page_is_not_written_when_opf_cannot_reference_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "broken.epub")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "META-INF/container.xml",
                    """<container><rootfiles>
                    <rootfile full-path="content.opf"/>
                    </rootfiles></container>""",
                )
                archive.writestr("content.opf", "<package><metadata/></package>")

            self.assertFalse(append_about_page(path, "zh-Hans"))

            with zipfile.ZipFile(path) as archive:
                self.assertFalse(
                    any("trans-novel-about" in name for name in archive.namelist())
                )

    def test_bilingual_rewrite_removes_temporary_file_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "book.epub")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "ch0.xhtml",
                    "<html><head></head><body><p>text</p></body></html>",
                )

            with (
                patch(
                    "trans_novel.assemble.writer.os.replace",
                    side_effect=OSError("replace failed"),
                ),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                _inject_bilingual_style(path, {"ch0.xhtml"}, "zh-Hans")

            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_pipeline_passes_about_page_config_to_writer(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.output.about_page = False
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))

            result = orch.run_all(txt, out_format="epub")

            with zipfile.ZipFile(result["output"]) as z:
                self.assertFalse(
                    any(name.endswith("trans-novel-about.xhtml") for name in z.namelist())
                )

    def test_txt_input_to_epub(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="epub")
            self.assertTrue(out.endswith(".epub"))
            self.assertEqual(os.path.basename(out), "novel.zh.epub")
            self.assertEqual(os.path.dirname(out), os.path.join(d, "output"))
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                about_name = next(
                    name for name in names if name.endswith("trans-novel-about.xhtml")
                )
                self.assertIn("关于此翻译", z.read(about_name).decode("utf-8"))
            # 重新解析生成的 EPUB，应能读出章节且含译文
            doc = load_document(out, "ja", "zh")
            self.assertGreaterEqual(len(doc.chapters), 2)
            alltext = "".join(s.source for c in doc.chapters for s in c.text_segments)
            self.assertIn("润", alltext)

    def test_english_target_controls_epub_name_metadata_and_about_page(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            manifest = store.load_manifest()
            manifest["target_lang"] = "en"
            store.save_manifest(manifest)

            out = assemble(store, txt, out_format="epub")

            self.assertEqual(os.path.basename(out), "novel.en.epub")
            with zipfile.ZipFile(out) as archive:
                opf_name = next(
                    name for name in archive.namelist() if name.endswith(".opf")
                )
                opf = BeautifulSoup(archive.read(opf_name), "xml")
                language_element = opf.find("language")
                self.assertIsNotNone(language_element)
                assert language_element is not None
                self.assertEqual(language_element.get_text(strip=True), "en")

                chapter_name = next(
                    name for name in archive.namelist() if name.endswith("/ch0.xhtml")
                )
                chapter = archive.read(chapter_name).decode("utf-8")
                self.assertIn('xml:lang="en"', chapter)

                about_name = next(
                    name
                    for name in archive.namelist()
                    if name.endswith("trans-novel-about.xhtml")
                )
                about = archive.read(about_name).decode("utf-8")
                self.assertIn("About This Translation", about)
                self.assertIn('xml:lang="en"', about)
                self.assertNotIn("关于此翻译", about)

    def test_about_page_is_localized_by_target_language(self):
        chinese = about_xhtml("zh-Hans").decode("utf-8")
        english = about_xhtml("en").decode("utf-8")

        self.assertIn("关于此翻译", chinese)
        self.assertIn("About This Translation", english)
        self.assertNotIn("翻译为中文", english)


class TestAssembleEpub(unittest.TestCase):
    def test_opf_export_keeps_only_the_target_language(self):
        opf = b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <metadata>
    <dc:title>Book</dc:title>
    <dc:language>ja</dc:language>
    <dc:language>fr</dc:language>
  </metadata>
</package>"""

        rewritten = _rewrite_opf_metadata(
            opf,
            book_title="",
            lang="en",
            force_horizontal=False,
        )
        soup = BeautifulSoup(rewritten, "xml")
        languages = soup.find_all(["dc:language", "language"])

        self.assertEqual([item.get_text() for item in languages], ["en"])

    def test_nested_fragment_id_survives_textual_markup_flattening(self):
        html = '<html><body><h2><span id="inside">Section</span></h2></body></html>'
        title, segments, template = annotate_epub_resource(html, 0, "chapter.xhtml")
        segments[0].target = "章节"
        chapter = Chapter(
            index=0,
            title=title,
            segments=segments,
            href="chapter.xhtml",
            template=template,
        )

        rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")

        marker = rendered.find(id="inside")
        self.assertIsInstance(marker, Tag)
        heading = rendered.find("h2")
        self.assertIsInstance(heading, Tag)
        assert isinstance(heading, Tag)
        self.assertEqual(heading.get_text(), "章节")

    def test_epub_render_flattens_textual_inline_markup(self):
        html = """<html><body>
<p><em>Hello</em> <a href="note.xhtml">world</a></p>
<p><ruby>漢字<rt>かんじ</rt></ruby>です</p>
</body></html>"""
        title, segments, template = annotate_epub_resource(html, 0, "chapter.xhtml")
        segments[0].target = "你好世界"
        segments[1].target = "汉字如此"
        chapter = Chapter(
            index=0,
            title=title,
            segments=segments,
            href="chapter.xhtml",
            template=template,
        )

        rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
        paragraphs = rendered.find_all("p")

        self.assertEqual(paragraphs[0].get_text(), "你好世界")
        self.assertEqual(paragraphs[1].get_text(), "汉字如此")
        self.assertIsNone(paragraphs[0].find("em"))
        self.assertIsNone(paragraphs[0].find("a"))
        self.assertIsNone(paragraphs[1].find("ruby"))

    def test_rewrite_html_honors_declared_encoding_and_emits_utf8(self):
        source = (
            '<?xml version="1.0" encoding="Shift_JIS"?>'
            "<html><body><p>日本語</p></body></html>"
        ).encode("shift_jis")

        output = _rewrite_html_document(
            source,
            lang="zh-Hans",
            force_horizontal=False,
        )
        decoded = output.decode("utf-8")

        self.assertIn("日本語", decoded)
        self.assertIn('encoding="utf-8"', decoded)
        self.assertIn('lang="zh-Hans"', decoded)

    def test_epub_export_rebuilds_inline_layout_without_persisted_meta(self):
        with tempfile.TemporaryDirectory() as d:
            epub = os.path.join(d, "inline.epub")
            write_inline_sample_epub(epub)
            store, _ = _run(epub, os.path.join(d, "state"))

            persisted = store.load_chapter(0)
            inline_segments = [s for s in persisted.segments if "epub_inline" in s.meta]
            self.assertEqual(inline_segments, [])

            output = assemble(store, epub, out_format="epub", about_page=False)
            with zipfile.ZipFile(output) as archive:
                rendered = BeautifulSoup(
                    archive.read("OEBPS/ch1.xhtml"),
                    "html.parser",
                )
                image_data = archive.read("OEBPS/image.jpg")

        paragraph = rendered.find("p", class_="Textbody")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        image = paragraph.find("img")
        self.assertIsInstance(image, Tag)
        assert isinstance(image, Tag)
        self.assertEqual(image.get("src"), "image.jpg")
        self.assertEqual(image_data, b"inline-image")
        self.assertIsNotNone(rendered.find(id="kobo.1.1"))
        self.assertIsNone(rendered.select_one("[data-tn-inline-id]"))

    def test_epub_export_rejects_source_state_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = os.path.join(directory, "inline.epub")
            write_inline_sample_epub(epub)
            store, _ = _run(epub, os.path.join(directory, "state"))
            chapter = store.load_chapter(0)
            chapter.segments[0].source += " changed"
            store.save_chapter(chapter)

            with self.assertRaisesRegex(ValueError, "内容已变化"):
                assemble(
                    store,
                    epub,
                    out_format="epub",
                    about_page=False,
                )

    def test_epub_render_restores_inline_images_and_breaks(self):
        html = """<html><body>
<p class="Textbody"><img src="before.jpg"/>Avant<br/>Après<img src="after.jpg"/></p>
<p class="illustration"><img src="standalone.jpg"/></p>
</body></html>"""
        title, segments, template = annotate_epub_resource(
            html,
            0,
            "chapter.xhtml",
        )
        segments[0].target = "甲乙丙丁"
        chapter = Chapter(
            index=0,
            title=title,
            segments=segments,
            href="chapter.xhtml",
            template=template,
        )

        rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")

        paragraph = rendered.find("p", class_="Textbody")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        self.assertEqual(paragraph.get_text(), "甲乙丙丁")
        self.assertEqual(
            [image.get("src") for image in paragraph.find_all("img")],
            ["before.jpg", "after.jpg"],
        )
        self.assertIsNotNone(paragraph.find("br"))
        self.assertEqual(
            [
                child.name if isinstance(child, Tag) else str(child)
                for child in paragraph.children
            ],
            ["img", "甲乙", "br", "丙丁", "img"],
        )
        self.assertIsNone(rendered.select_one("[data-tn-inline-id]"))
        standalone = rendered.find("p", class_="illustration")
        self.assertIsInstance(standalone, Tag)
        assert isinstance(standalone, Tag)
        standalone_image = standalone.find("img")
        self.assertIsInstance(standalone_image, Tag)
        assert isinstance(standalone_image, Tag)
        self.assertEqual(standalone_image.get("src"), "standalone.jpg")

    def test_bilingual_render_does_not_duplicate_inline_images(self):
        html = """<html><body>
<p><img src="illustration.jpg"/>Texte original.</p>
</body></html>"""
        title, segments, template = annotate_epub_resource(
            html,
            0,
            "chapter.xhtml",
        )
        segments[0].target = "译文。"
        chapter = Chapter(
            index=0,
            title=title,
            segments=segments,
            href="chapter.xhtml",
            template=template,
        )

        rendered = BeautifulSoup(
            _render_chapter_html(chapter, bilingual=True),
            "html.parser",
        )

        self.assertEqual(len(rendered.find_all("img")), 1)
        source = rendered.find(class_="tn-source")
        self.assertIsInstance(source, Tag)
        assert isinstance(source, Tag)
        self.assertIsNone(source.find("img"))

    def test_epub_template_rebuild(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub")
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as z:
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
                about = z.read("OEBPS/trans-novel-about.xhtml").decode("utf-8")
                opf = BeautifulSoup(z.read("OEBPS/content.opf"), "xml")
            self.assertIn("润0", html)            # 译文已替换
            self.assertNotIn("data-tn-id", html)  # 占位标记已清除
            self.assertNotIn("綾小路は教室", html)  # 原文已被替换
            self.assertIn("关于此翻译", about)
            about_item = opf.find("item", href="trans-novel-about.xhtml")
            self.assertIsNotNone(about_item)
            assert about_item is not None
            spine = opf.find("spine")
            self.assertIsNotNone(spine)
            assert spine is not None
            spine_items = spine.find_all("itemref")
            self.assertEqual(spine_items[-1].get("idref"), about_item.get("id"))

    def test_about_page_can_be_disabled_for_template_epub(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))

            out = assemble(store, ep, out_format="epub", about_page=False)

            with zipfile.ZipFile(out) as z:
                self.assertFalse(
                    any(name.endswith("trans-novel-about.xhtml") for name in z.namelist())
                )

    def test_vertical_epub_is_exported_as_horizontal_chinese(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "vertical.epub")
            _write_vertical_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                opf = z.read("OEBPS/content.opf").decode("utf-8")
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertIn("<dc:language>zh-Hans</dc:language>", opf)
            self.assertIn('page-progression-direction="ltr"', opf)
            self.assertIn("writing-mode: horizontal-tb", html)
            self.assertIn('lang="zh-Hans"', html)
            self.assertNotIn('class="vrtl"', html)


class TestTitleTranslation(unittest.TestCase):
    def test_invalid_title_count_stops_instead_of_saving_partial_toc(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.epub")
            write_sample_epub(source)
            document = load_document(source, "ja", "zh")
            store = RunStore(os.path.join(directory, "state"))
            manifest = store.stage_document(document)
            manifest["meta"]["toc_entries"] = [
                {
                    "entry_id": "nav.xhtml:0",
                    "toc_path": "nav.xhtml",
                    "node_index": 0,
                    "title": "Unlinked title",
                }
            ]
            for chapter_meta in manifest["chapters"]:
                chapter = store.load_chapter(chapter_meta["index"])
                for segment in chapter.segments:
                    segment.target = "译文"
                store.save_chapter(chapter)
            store.save_manifest(manifest)
            client = FakeClient(handler=routing_handler)
            orchestrator = Orchestrator(_config(directory), client=client)
            glossary = GlossaryStore(store.glossary_path)
            try:
                with (
                    patch.object(
                        client,
                        "complete_json",
                        return_value={"titles": []},
                    ),
                    self.assertRaisesRegex(RuntimeError, "invalid number"),
                ):
                    orchestrator._translate_titles(store, glossary)
            finally:
                glossary.close()

            entry = store.load_manifest()["meta"]["toc_entries"][0]
            self.assertNotIn("title_translated", entry)

    def test_ncx_with_xml_extension_is_rewritten_as_ncx(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "toc-xml.epub")
            output = os.path.join(directory, "translated.epub")
            write_nested_toc_epub(source, ncx_filename="toc.xml")
            document = load_document(source, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            manifest = store.stage_document(document)

            for chapter_meta in manifest["chapters"]:
                chapter = store.load_chapter(chapter_meta["index"])
                for segment in chapter.segments:
                    segment.target = f"T{chapter.index}-{segment.index}"
                store.save_chapter(chapter)
            translated_titles = ["第一部", "第一节", "第二部", "第二节"]
            for entry, target in zip(
                manifest["meta"]["toc_entries"],
                translated_titles,
            ):
                entry["title_translated"] = target
            store.save_manifest(manifest)

            assemble(
                store,
                source,
                out_path=output,
                out_format="epub",
                about_page=False,
            )

            with zipfile.ZipFile(output) as archive:
                toc = BeautifulSoup(archive.read("OEBPS/toc.xml"), "xml")

        self.assertEqual(
            [node.get_text(strip=True) for node in toc.find_all("text")],
            translated_titles,
        )

    def test_all_toc_entries_reuse_linked_heading_translations(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "nested.epub")
            write_nested_toc_epub(source)

            store, _config_value = _run(source, os.path.join(d, "state"))
            manifest = store.load_manifest()
            entries = manifest["meta"]["toc_entries"]
            self.assertEqual(len(entries), 4)
            self.assertEqual([entry["depth"] for entry in entries], [0, 1, 0, 1])
            self.assertTrue(all(entry.get("title_translated") for entry in entries))
            self.assertEqual(len(manifest["chapters"]), 2)

            targets_by_anchor = {
                segment.anchor: segment.target
                for chapter_meta in manifest["chapters"]
                for segment in store.load_chapter(chapter_meta["index"]).segments
                if segment.anchor
            }
            for entry in entries:
                self.assertEqual(
                    entry["title_translated"],
                    targets_by_anchor[entry["segment_anchor"]],
                )

    def test_same_xhtml_logical_chapters_and_toc_entries_are_all_written(self):
        for toc_kind in ("ncx", "nav"):
            with self.subTest(toc_kind=toc_kind), tempfile.TemporaryDirectory() as d:
                source = os.path.join(d, f"nested-{toc_kind}.epub")
                output = os.path.join(d, f"translated-{toc_kind}.epub")
                write_nested_toc_epub(
                    source,
                    toc_kind=toc_kind,
                    nav_in_spine=toc_kind == "nav",
                )
                document = load_document(source, "en", "zh")
                store = RunStore(os.path.join(d, "state"))
                manifest = store.stage_document(document)

                expected_targets: list[str] = []
                for chapter_meta in manifest["chapters"]:
                    chapter = store.load_chapter(chapter_meta["index"])
                    for segment in chapter.segments:
                        segment.target = f"C{chapter.index}S{segment.index}"
                        expected_targets.append(segment.target)
                    store.save_chapter(chapter)
                toc_targets = ["第一部", "第一节", "第二部", "第二节"]
                for entry, target in zip(manifest["meta"]["toc_entries"], toc_targets):
                    entry["title_translated"] = target
                store.save_manifest(manifest)

                assemble(
                    store,
                    source,
                    out_path=output,
                    out_format="epub",
                    about_page=False,
                )

                with zipfile.ZipFile(output) as archive:
                    body = archive.read("OEBPS/body.xhtml").decode("utf-8")
                    toc_name = "OEBPS/toc.ncx" if toc_kind == "ncx" else "OEBPS/nav.xhtml"
                    toc = BeautifulSoup(
                        archive.read(toc_name),
                        "xml" if toc_kind == "ncx" else "html.parser",
                    )

                for target in expected_targets:
                    self.assertIn(target, body)
                self.assertNotIn("data-tn-id", body)
                if toc_kind == "ncx":
                    labels = [node.get_text(strip=True) for node in toc.find_all("text")]
                    hrefs = [node.get("src") for node in toc.find_all("content")]
                else:
                    labels = [node.get_text(strip=True) for node in toc.find_all("a")]
                    hrefs = [node.get("href") for node in toc.find_all("a")]
                self.assertEqual(labels, toc_targets)
                self.assertEqual(
                    hrefs,
                    [
                        "body.xhtml#part-1",
                        "body.xhtml#section-1",
                        "body.xhtml#part-2",
                        "body.xhtml#section-2",
                    ],
                )

    def test_manifest_keeps_book_title_and_translates_chapter_titles(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            # 书名不翻译；章节标题译出并写回 manifest（fake：标题0/1）
            m = store.load_manifest()
            self.assertNotIn("title_translated", m)
            self.assertTrue(all(c.get("title_translated") for c in m["chapters"]))
            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                opf = z.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn("サンプル小説", opf)       # OPF 书名保持原文
            self.assertIn("<dc:language>zh-Hans</dc:language>", opf)
            self.assertEqual(os.path.basename(out), "novel.zh.epub")

    def test_rewrite_nav_and_ncx_labels(self):
        from trans_novel.assemble.writer import _rewrite_toc

        nav = (b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>'
               b'<nav epub:type="toc"><ol>'
               b'<li><a href="ch1.xhtml">\xe7\xac\xac\xe4\xb8\x80\xe7\xab\xa0</a></li>'
               b'</ol></nav></body></html>')
        out = _rewrite_toc(nav, {"ch1.xhtml": "第一章译名"}, is_ncx=False)
        self.assertIn("第一章译名", out.decode("utf-8"))

        ncx = (b'<?xml version="1.0"?><ncx><navMap><navPoint>'
               b'<navLabel><text>old</text></navLabel>'
               b'<content src="text/ch1.xhtml#x"/></navPoint></navMap></ncx>')
        out2 = _rewrite_toc(ncx, {"ch1.xhtml": "第一章译名"}, is_ncx=True)
        dec = out2.decode("utf-8")
        self.assertIn("第一章译名", dec)
        self.assertNotIn(">old<", dec)


class TestReport(unittest.TestCase):
    def test_report_summary(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            g = GlossaryStore(store.glossary_path)
            report = build_report(store, g)
            g.close()
            s = report["summary"]
            self.assertEqual(s["chapters_done"], s["chapters_total"])
            self.assertEqual(s["empty_targets"], 0)  # 全部段都有译文
            self.assertGreaterEqual(s["terms"], 1)
            self.assertNotIn("low_confidence_terms", report)


class TestConsistency(unittest.TestCase):
    def test_consistency_reports_issues(self):
        from trans_novel.agents.consistency import ConsistencyChecker

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, cfg = _run(txt, os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "一致性审查员" in messages[0]["content"]:
                    return json.dumps({"issues": [
                        {"type": "terminology", "detail": "X 译法不一致", "where": "第1章"}
                    ]}, ensure_ascii=False)
                return "{}"

            g = GlossaryStore(store.glossary_path)
            checker = ConsistencyChecker(FakeClient(handler=handler), cfg)
            issues = checker.check(store, g)
            g.close()
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["type"], "terminology")


if __name__ == "__main__":
    unittest.main()
