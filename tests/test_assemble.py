"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from bs4 import BeautifulSoup
from bs4.element import Tag

from tests.fake_llm import routing_handler
from tests.sample_data import (
    write_inline_sample_epub,
    write_nested_toc_epub,
    write_sample_epub,
    write_sample_txt,
)
from trans_novel.assemble.about import append_about_page
from trans_novel.assemble.report import build_report
from trans_novel.assemble.writer import (
    _inject_bilingual_style,
    _render_chapter_html,
    _render_segments_html,
    _rewrite_html_document,
    assemble,
)
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.epub_reader import annotate_epub_resource
from trans_novel.ingest.models import Chapter, Segment
from trans_novel.ingest.segmenter import load_document
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore

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


def _write_linked_notes_epub(path: str) -> None:
    """写入正文与注释分处两个 XHTML、且具有双向 fragment 链接的 EPUB。"""
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    opf = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Linked Notes</dc:title><dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="body" href="text/body.xhtml" media-type="application/xhtml+xml"/>
    <item id="notes" href="notes/notes.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="body"/><itemref idref="notes"/></spine>
</package>"""
    body = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<p><a href="../notes/notes.xhtml#note-1">border tunnel
<sup id="key-1">1</sup></a> opens.</p></body></html>"""
    notes = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<p id="note-1"><a href="../text/body.xhtml#key-1">1 border tunnel</a>:
the long tunnel at the border.</p></body></html>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/text/body.xhtml", body)
        archive.writestr("OEBPS/notes/notes.xhtml", notes)


def _config(state_dir: str):
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
            },
            "pipeline": {"review": True, "polish": True},
            "paths": {"state_dir": state_dir},
        }
    )


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
                self.assertFalse(any("trans-novel-about" in name for name in archive.namelist()))

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
                    "trans_novel.assemble.epub_writer.os.replace",
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


class TestAssembleEpub(unittest.TestCase):
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

    def test_epub_render_flattens_markup_but_preserves_internal_link(self):
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

        self.assertEqual(paragraphs[0].get_text().replace("↩", ""), "你好世界")
        self.assertEqual(paragraphs[1].get_text(), "汉字如此")
        self.assertIsNone(paragraphs[0].find("em"))
        link = paragraphs[0].find("a")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertEqual(link.get("href"), "note.xhtml")
        self.assertEqual(link.get_text(), "↩")
        self.assertIsNone(paragraphs[1].find("ruby"))

    def test_epub_render_restores_point_annotation_link_at_aligned_offset(self):
        target = "你好，世界"
        template = """<html><body><p data-tn-id="tn1_0">Hello<sup
data-tn-annotation-id="ann-0"><a class="noteref" href="notes.xhtml#n1"
id="ref-1">1</a></sup> world</p></body></html>"""
        segment = Segment(
            index=0,
            source="Hello world",
            target=target,
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "version": 1,
                    "source_length": 11,
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "point",
                            "source_start": 5,
                            "source_end": 5,
                            "source_text": "",
                            "marker_text": "1",
                        }
                    ],
                    "target_digest": hashlib.sha256(target.encode()).hexdigest(),
                    "placements": [
                        {
                            "id": "ann-0",
                            "target_start": 2,
                            "target_end": 2,
                            "status": "aligned",
                            "method": "model",
                        }
                    ],
                }
            },
        )

        rendered = BeautifulSoup(
            _render_segments_html(template, [segment]),
            "html.parser",
        )

        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        reference = paragraph.find("a")
        self.assertIsInstance(reference, Tag)
        assert isinstance(reference, Tag)
        self.assertEqual(reference.get("href"), "notes.xhtml#n1")
        self.assertEqual(reference.get("id"), "ref-1")
        self.assertIn("noteref", reference.get_attribute_list("class"))
        reference_parent = reference.parent
        self.assertIsInstance(reference_parent, Tag)
        assert isinstance(reference_parent, Tag)
        self.assertEqual(reference_parent.name, "sup")
        self.assertEqual(paragraph.get_text().replace("1", ""), target)
        self.assertIsNone(rendered.select_one("[data-tn-annotation-id]"))

    def test_epub_render_restores_css_superscript_annotation_wrapper(self):
        target = "巴门尼德留下了一份遗产。"
        template = """<html><body><p data-tn-id="tn1_0">Parmenides left a legacy.<span
class="superscript" data-tn-annotation-id="ann-0"><a class="nounder"
href="intro.html#intronotes_1" id="intronotes1">1</a></span></p></body></html>"""
        segment = Segment(
            index=0,
            source="Parmenides left a legacy.",
            target=target,
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "version": 1,
                    "source_length": len("Parmenides left a legacy."),
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "point",
                            "source_start": len("Parmenides left a legacy."),
                            "source_end": len("Parmenides left a legacy."),
                            "source_text": "",
                            "marker_text": "1",
                        }
                    ],
                    "target_digest": hashlib.sha256(target.encode()).hexdigest(),
                    "placements": [
                        {
                            "id": "ann-0",
                            "target_start": len(target),
                            "target_end": len(target),
                            "status": "aligned",
                            "method": "model",
                        }
                    ],
                }
            },
        )

        rendered = BeautifulSoup(_render_segments_html(template, [segment]), "html.parser")

        marker = rendered.select_one("span.superscript")
        self.assertIsInstance(marker, Tag)
        assert isinstance(marker, Tag)
        link = marker.find("a")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertEqual(link.get("href"), "intro.html#intronotes_1")
        self.assertEqual(link.get("id"), "intronotes1")
        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        self.assertEqual(paragraph.get_text().replace("1", ""), target)

    def test_epub_render_restores_range_annotation_around_target_phrase(self):
        source_phrase = "border tunnel"
        target_phrase = "国境隧道"
        target = "火车穿过国境隧道后停下。"
        start = target.index(target_phrase)
        end = start + len(target_phrase)
        template = """<html><body><p data-tn-id="tn1_0"><a class="cyu"
data-tn-annotation-id="ann-0" href="notes.xhtml#note-1" id="ref-1">border
tunnel<sup class="key" id="key-1">〔＊1〕</sup></a> opens.</p></body></html>"""
        segment = Segment(
            index=0,
            source="border tunnel opens.",
            target=target,
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "version": 1,
                    "source_length": 20,
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "range",
                            "source_start": 0,
                            "source_end": len(source_phrase),
                            "source_text": source_phrase,
                            "marker_text": "〔＊1〕",
                        }
                    ],
                    "target_digest": hashlib.sha256(target.encode()).hexdigest(),
                    "placements": [
                        {
                            "id": "ann-0",
                            "target_start": start,
                            "target_end": end,
                            "status": "aligned",
                            "method": "model",
                        }
                    ],
                }
            },
        )

        rendered = BeautifulSoup(
            _render_segments_html(template, [segment]),
            "html.parser",
        )

        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        link = paragraph.find("a")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertEqual(link.get("href"), "notes.xhtml#note-1")
        self.assertEqual(link.get("id"), "ref-1")
        self.assertIn("cyu", link.get_attribute_list("class"))
        marker = link.find("sup")
        self.assertIsInstance(marker, Tag)
        assert isinstance(marker, Tag)
        self.assertEqual(marker.get("id"), "key-1")
        self.assertIn("key", marker.get_attribute_list("class"))
        self.assertEqual(link.get_text().replace("〔＊1〕", ""), target_phrase)
        self.assertEqual(paragraph.get_text().replace("〔＊1〕", ""), target)
        self.assertNotIn(source_phrase, paragraph.get_text())
        self.assertIsNone(rendered.select_one("[data-tn-annotation-id]"))

    def test_epub_render_keeps_image_inside_restored_range_link(self):
        html = """<html><body><p>See <a href="#figure"><span id="semantic-id">linked
<img src="thumb.png"/> phrase</span><sup id="ref-mark">*</sup></a> now.</p></body></html>"""
        _title, segments, template = annotate_epub_resource(html, 0, "body.xhtml")
        segment = segments[0]
        segment.target = "请看链接短语。"
        phrase = "链接短语"
        start = segment.target.index(phrase)
        item = segment.meta["epub_annotations"]["items"][0]
        segment.meta["epub_annotations"].update(
            {
                "target_digest": hashlib.sha256(segment.target.encode()).hexdigest(),
                "placements": [
                    {
                        "id": item["id"],
                        "target_start": start,
                        "target_end": start + len(phrase),
                        "status": "aligned",
                        "method": "llm_markers",
                    }
                ],
            }
        )

        rendered = BeautifulSoup(_render_segments_html(template, [segment]), "html.parser")

        link = rendered.find("a", href="#figure")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertIn(phrase, link.get_text())
        self.assertIsNotNone(link.find("img", src="thumb.png"))
        self.assertIsNotNone(rendered.find(id="semantic-id"))
        self.assertIsNotNone(link.find("sup", id="ref-mark"))
        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        self.assertEqual(paragraph.get_text().replace("*", ""), segment.target)

    def test_epub_render_degrades_stale_range_to_clickable_end_marker(self):
        target = "列车驶过隧道。"
        template = """<html><body><p data-tn-id="tn1_0"><a class="cyu"
data-tn-annotation-id="ann-0" href="notes.xhtml#note-1">border tunnel
<sup id="key-1">〔＊1〕</sup></a> opens.</p></body></html>"""
        segment = Segment(
            index=0,
            source="border tunnel opens.",
            target=target,
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "version": 1,
                    "source_length": 20,
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "range",
                            "source_start": 0,
                            "source_end": 13,
                            "source_text": "border tunnel",
                            "marker_text": "〔＊1〕",
                        }
                    ],
                    "target_digest": "stale",
                    "placements": [
                        {
                            "id": "ann-0",
                            "target_start": 0,
                            "target_end": 2,
                            "status": "aligned",
                            "method": "model",
                        }
                    ],
                }
            },
        )

        rendered = BeautifulSoup(
            _render_segments_html(template, [segment]),
            "html.parser",
        )

        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        link = paragraph.find("a")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertEqual(link.get("href"), "notes.xhtml#note-1")
        self.assertEqual(link.get_text(), "〔＊1〕")
        self.assertEqual(paragraph.get_text().replace("〔＊1〕", ""), target)
        self.assertNotIn("border tunnel", paragraph.get_text())
        self.assertIsNone(rendered.select_one("[data-tn-annotation-id]"))

    def test_epub_render_separates_multiple_fallback_markers_with_a_comma(self):
        target = "正文内容。"
        template = (
            '<html><body><p data-tn-id="tn1_0">Foo'
            '<a data-tn-annotation-id="ann-0" href="notes.xhtml#n1">11</a>'
            " bar"
            '<a data-tn-annotation-id="ann-1" href="notes.xhtml#n2">12</a>'
            " baz.</p></body></html>"
        )
        segment = Segment(
            index=0,
            source="Foo bar baz.",
            target=target,
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "version": 1,
                    "source_length": len("Foo bar baz."),
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "point",
                            "source_start": 3,
                            "source_end": 3,
                            "source_text": "",
                            "marker_text": "11",
                        },
                        {
                            "id": "ann-1",
                            "mode": "point",
                            "source_start": 7,
                            "source_end": 7,
                            "source_text": "",
                            "marker_text": "12",
                        },
                    ],
                    # 过期摘要强制两条注释都降级为段末回退标记。
                    "target_digest": "stale",
                    "placements": [],
                }
            },
        )

        rendered = BeautifulSoup(
            _render_segments_html(template, [segment]),
            "html.parser",
        )

        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        links = paragraph.find_all("a")
        self.assertEqual([link.get_text() for link in links], ["11", "12"])
        # 两个降级标记之间必须有顿号分隔，否则连写成无法轨读的 "1112"。
        self.assertIn("11、12", paragraph.get_text())
        self.assertNotIn("1112", paragraph.get_text())

    def test_epub_render_keeps_untranslated_annotation_at_source_position(self):
        html = """<html><body><p>Before<sup><a class="noteref"
href="#n1" id="ref-1">1</a></sup> after.</p></body></html>"""
        _title, segments, template = annotate_epub_resource(html, 0, "body.xhtml")

        rendered = BeautifulSoup(
            _render_segments_html(template, segments),
            "html.parser",
        )

        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        reference = paragraph.find("a", href="#n1")
        self.assertIsInstance(reference, Tag)
        assert isinstance(reference, Tag)
        self.assertEqual(reference.get("id"), "ref-1")
        self.assertEqual(paragraph.get_text(), "Before1 after.")
        self.assertLess(str(paragraph).index("<sup"), str(paragraph).index(" after."))
        self.assertIsNone(rendered.select_one("[data-tn-annotation-id]"))
        self.assertIsNone(rendered.select_one("[data-tn-inline-id]"))

    def test_bilingual_source_keeps_annotation_at_original_position(self):
        html = """<html><body><p>See <a class="annotated" href="#note-1"
id="ref-1">border tunnel<sup class="key" id="key-1">〔＊1〕</sup></a>
now.</p></body></html>"""
        _title, segments, template = annotate_epub_resource(html, 0, "body.xhtml")
        segments[0].target = "请看国境隧道。"

        rendered = BeautifulSoup(
            _render_segments_html(template, segments, bilingual=True, source_lang="en"),
            "html.parser",
        )

        target = rendered.select_one("p:not([class])")
        source = rendered.find("p", class_="tn-source")
        self.assertIsInstance(target, Tag)
        self.assertIsInstance(source, Tag)
        assert isinstance(target, Tag)
        assert isinstance(source, Tag)
        source_reference = source.find("a", href="#note-1")
        target_reference = target.find("a", href="#note-1")
        self.assertIsInstance(source_reference, Tag)
        self.assertIsInstance(target_reference, Tag)
        assert isinstance(source_reference, Tag)
        assert isinstance(target_reference, Tag)
        self.assertEqual(source_reference.get_text().replace("〔＊1〕", ""), "border tunnel")
        self.assertEqual(
            source.get_text().replace("\n", " ").split(), ["See", "border", "tunnel〔＊1〕", "now."]
        )
        self.assertEqual(source.get("id"), "tn-source-tn0_0")
        self.assertIsNone(source_reference.get("id"))
        self.assertEqual(target_reference.get("id"), "ref-1")
        self.assertIsNone(rendered.select_one("[data-tn-annotation-id]"))

    def test_bilingual_internal_links_stay_with_their_language(self):
        """同 XHTML 的原文和译文分别使用自己的脚注跳转闭环。"""
        html = """<html><body>
<p id="body"><a id="ref-1" href="#note-1">border tunnel
<sup id="key-1">1</sup></a> opens.</p>
<p id="note-1"><a href="#key-1">1 border tunnel</a>: a note.</p>
</body></html>"""
        for order in ("target_first", "source_first"):
            with self.subTest(order=order):
                _title, segments, template = annotate_epub_resource(
                    html,
                    0,
                    "body.xhtml",
                )
                segments[0].target = "国境隧道开启了。"
                segments[1].target = "国境隧道：一条注释。"
                rendered = BeautifulSoup(
                    _render_segments_html(
                        template,
                        segments,
                        bilingual=True,
                        order=order,
                        source_lang="en",
                        resource_href="body.xhtml",
                    ),
                    "html.parser",
                )

                target_body = rendered.find("p", id="body")
                target_note = rendered.find("p", id="note-1")
                source_body = rendered.find("p", id="tn-source-tn0_0")
                source_note = rendered.find("p", id="tn-source-tn0_1")
                for node in (target_body, target_note, source_body, source_note):
                    self.assertIsInstance(node, Tag)
                assert isinstance(target_body, Tag)
                assert isinstance(target_note, Tag)
                assert isinstance(source_body, Tag)
                assert isinstance(source_note, Tag)

                self.assertIsNotNone(target_body.find("a", href="#note-1"))
                self.assertIsNotNone(target_note.find("a", href="#key-1"))
                self.assertIsNotNone(source_body.find("a", href="#tn-source-tn0_1"))
                self.assertIsNotNone(source_note.find("a", href="#tn-source-tn0_0"))
                ids = [str(node["id"]) for node in rendered.find_all(id=True)]
                self.assertEqual(len(ids), len(set(ids)))
                for link in rendered.find_all("a", href=True):
                    href = link.get("href")
                    if isinstance(href, str) and href.startswith("#"):
                        self.assertIn(href[1:], ids)

    def test_bilingual_source_anchor_avoids_existing_id_collision(self):
        """原书已有 synthetic ID 同名项时，为原文锚点稳定追加序号。"""
        html = """<html><body>
<span id="tn-source-tn0_0"></span>
<p id="body"><a data-tn-annotation-id="ref" href="#note-1">body</a></p>
<p id="note-1"><a data-tn-annotation-id="back" href="#body">note</a></p>
</body></html>"""
        _title, segments, template = annotate_epub_resource(html, 0, "body.xhtml")
        segments[0].target = "正文"
        segments[1].target = "注释"

        rendered = BeautifulSoup(
            _render_segments_html(
                template,
                segments,
                bilingual=True,
                source_lang="en",
                resource_href="body.xhtml",
            ),
            "html.parser",
        )

        source_body = rendered.find("p", id="tn-source-tn0_0-2")
        source_note = rendered.find("p", id="tn-source-tn0_1")
        self.assertIsInstance(source_body, Tag)
        self.assertIsInstance(source_note, Tag)
        assert isinstance(source_body, Tag)
        assert isinstance(source_note, Tag)
        self.assertIsNotNone(source_body.find("a", href="#tn-source-tn0_1"))
        self.assertIsNotNone(source_note.find("a", href="#tn-source-tn0_0-2"))
        ids = [str(node["id"]) for node in rendered.find_all(id=True)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_epub_render_merges_fresh_nodes_with_persisted_alignment(self):
        target = "译文"
        template = """<html><body><p data-tn-id="tn1_0">source<sup
data-tn-annotation-id="ann-0"><a href="notes.xhtml#n1">1</a></sup></p>
</body></html>"""
        segment = Segment(
            index=0,
            source="source",
            target=target,
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "target_digest": hashlib.sha256(target.encode()).hexdigest(),
                    "placements": [
                        {
                            "id": "ann-0",
                            "target_start": 2,
                            "target_end": 2,
                            "status": "aligned",
                            "method": "model",
                        }
                    ],
                }
            },
        )
        fresh_meta: dict[str, dict[str, object]] = {
            "tn1_0": {
                "epub_annotations": {
                    "version": 1,
                    "source_length": 6,
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "point",
                            "source_start": 6,
                            "source_end": 6,
                            "source_text": "",
                            "marker_text": "1",
                        }
                    ],
                }
            }
        }

        rendered = BeautifulSoup(
            _render_segments_html(
                template,
                [segment],
                render_meta_by_anchor=fresh_meta,
            ),
            "html.parser",
        )

        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        self.assertEqual(paragraph.get_text().replace("1", ""), target)
        link = paragraph.find("a")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertEqual(link.get("href"), "notes.xhtml#n1")

    def test_epub_render_keeps_marker_when_link_is_translation_block(self):
        template = """<html><body><ul><li><a data-tn-id="tn1_0"
data-tn-annotation-id="ann-0" href="chapter.xhtml#part">Chapter
<sup id="note-ref">1</sup></a></li></ul></body></html>"""
        segment = Segment(
            index=0,
            source="Chapter",
            target="章节",
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "version": 1,
                    "source_length": 7,
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "range",
                            "source_start": 0,
                            "source_end": 7,
                            "source_text": "Chapter",
                            "marker_text": "1",
                        }
                    ],
                }
            },
        )

        rendered = BeautifulSoup(
            _render_segments_html(template, [segment]),
            "html.parser",
        )

        link = rendered.find("a")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertEqual(link.get("href"), "chapter.xhtml#part")
        self.assertEqual(link.get_text().replace("1", "").strip(), "章节")
        marker = link.find("sup")
        self.assertIsInstance(marker, Tag)
        assert isinstance(marker, Tag)
        self.assertEqual(marker.get("id"), "note-ref")
        self.assertIsNone(rendered.select_one("[data-tn-annotation-id]"))

    def test_rewrite_html_honors_declared_encoding_and_emits_utf8(self):
        source = (
            '<?xml version="1.0" encoding="Shift_JIS"?><html><body><p>日本語</p></body></html>'
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
        segments[0].target = "甲乙"
        segments[1].target = "丙丁"
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
            [child.name if isinstance(child, Tag) else str(child) for child in paragraph.children],
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

    def test_epub_render_preserves_nested_list_links_and_blockquote_lines(self):
        html = """<html><body>
<ul><li><a href="#author">Author</a><ul>
<li><a href="chapter.xhtml#one">Chapter One</a></li>
<li><a href="chapter.xhtml#two">Chapter Two</a></li>
</ul></li></ul>
<blockquote><div>Dedication One</div><div>Dedication Two</div></blockquote>
</body></html>"""
        title, segments, template = annotate_epub_resource(html, 0, "contents.xhtml")
        for segment, target in zip(
            segments,
            ["作者", "第一章", "第二章", "献词一", "献词二"],
        ):
            segment.target = target
        chapter = Chapter(
            index=0,
            title=title,
            segments=segments,
            href="contents.xhtml",
            template=template,
        )

        rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")

        links = rendered.find_all("a")
        self.assertEqual(
            [link.get_text() for link in links],
            ["作者", "第一章", "第二章"],
        )
        self.assertEqual(
            [link.get("href") for link in links],
            ["#author", "chapter.xhtml#one", "chapter.xhtml#two"],
        )
        self.assertEqual(len(rendered.find_all("li")), 3)
        quote = rendered.find("blockquote")
        self.assertIsInstance(quote, Tag)
        assert isinstance(quote, Tag)
        self.assertEqual(
            [line.get_text() for line in quote.find_all("div", recursive=False)],
            ["献词一", "献词二"],
        )

    def test_epub_render_rebuilds_heading_breaks_from_translated_lines(self):
        html = """<html><body><h1>
Isaac Asimov<br/><br/>Tales of the Black Widowers<br/>
</h1></body></html>"""
        title, segments, template = annotate_epub_resource(html, 0, "title.xhtml")
        self.assertEqual(
            [segment.source for segment in segments],
            ["Isaac Asimov", "Tales of the Black Widowers"],
        )
        segments[0].target = "艾萨克·阿西莫夫"
        segments[1].target = "《黑鳏夫俱乐部故事》"
        chapter = Chapter(
            index=0,
            title=title,
            segments=segments,
            href="title.xhtml",
            template=template,
        )

        rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
        heading = rendered.find("h1")
        self.assertIsInstance(heading, Tag)
        assert isinstance(heading, Tag)
        self.assertEqual(len(heading.find_all("br")), 3)
        self.assertIsNone(rendered.select_one("[data-tn-line]"))
        self.assertEqual(
            [
                child.name if isinstance(child, Tag) else str(child)
                for child in heading.children
                if isinstance(child, Tag) or str(child).strip()
            ],
            ["艾萨克·阿西莫夫", "br", "br", "《黑鳏夫俱乐部故事》", "br"],
        )

    def test_bilingual_break_lines_keep_valid_paragraph_structure(self):
        html = "<html><body><p>First<br/>Second</p></body></html>"
        title, segments, template = annotate_epub_resource(html, 0, "lines.xhtml")
        segments[0].target = "第一"
        segments[1].target = "第二"
        chapter = Chapter(
            index=0,
            title=title,
            segments=segments,
            href="lines.xhtml",
            template=template,
        )

        rendered = BeautifulSoup(
            _render_chapter_html(chapter, bilingual=True),
            "html.parser",
        )
        paragraph = rendered.find("p")
        self.assertIsInstance(paragraph, Tag)
        assert isinstance(paragraph, Tag)
        self.assertIsNone(paragraph.find("p"))
        self.assertEqual(
            [source.get_text() for source in paragraph.select("span.tn-source")],
            ["First", "Second"],
        )

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

    def test_bilingual_cross_file_links_only_rewrite_source_fragments(self):
        """跨 XHTML 脚注保留相对路径，并分别闭合原文和译文链接。"""
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "linked-notes.epub")
            output_path = os.path.join(directory, "linked-notes-bi.epub")
            _write_linked_notes_epub(source_path)
            document = load_document(source_path, "en", "zh")
            store = RunStore(os.path.join(directory, "state"))
            manifest = store.stage_document(document)
            for chapter_meta in manifest["chapters"]:
                chapter = store.load_chapter(chapter_meta["index"])
                for segment in chapter.segments:
                    segment.target = f"译文 {chapter.index}-{segment.index}"
                store.save_chapter(chapter)
            manifest["initialized"] = True
            store.save_manifest(manifest)

            assemble(
                store,
                source_path,
                out_path=output_path,
                out_format="epub",
                bilingual=True,
                about_page=False,
            )

            with zipfile.ZipFile(output_path) as archive:
                body = BeautifulSoup(
                    archive.read("OEBPS/text/body.xhtml"),
                    "html.parser",
                )
                notes = BeautifulSoup(
                    archive.read("OEBPS/notes/notes.xhtml"),
                    "html.parser",
                )

        body_target = body.select_one("p:not([class])")
        notes_target = notes.select_one("p:not([class])")
        body_source = body.find("p", class_="tn-source")
        notes_source = notes.find("p", class_="tn-source")
        for node in (body_target, notes_target, body_source, notes_source):
            self.assertIsInstance(node, Tag)
        assert isinstance(body_target, Tag)
        assert isinstance(notes_target, Tag)
        assert isinstance(body_source, Tag)
        assert isinstance(notes_source, Tag)

        self.assertIsNotNone(body_target.find("a", href="../notes/notes.xhtml#note-1"))
        self.assertIsNotNone(notes_target.find("a", href="../text/body.xhtml#key-1"))
        body_source_id = str(body_source.get("id"))
        notes_source_id = str(notes_source.get("id"))
        self.assertIsNotNone(
            body_source.find(
                "a",
                href=f"../notes/notes.xhtml#{notes_source_id}",
            )
        )
        self.assertIsNotNone(
            notes_source.find(
                "a",
                href=f"../text/body.xhtml#{body_source_id}",
            )
        )

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
            self.assertIn("润0", html)  # 译文已替换
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
            self.assertIn("<dc:title>縦書き小説-wenyi-zh</dc:title>", opf)
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
                    orchestrator._translation.translate_titles(store, glossary)
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
            # 书名不翻译，导出时在原书名后追加 Wenyi 和目标语言标记
            self.assertIn("<dc:title>サンプル小説-wenyi-zh</dc:title>", opf)
            self.assertIn("<dc:language>zh-Hans</dc:language>", opf)
            self.assertEqual(os.path.basename(out), "novel.zh.epub")

    def test_rewrite_nav_and_ncx_labels(self):
        from trans_novel.assemble.writer import _rewrite_toc

        toc_path = "toc.xhtml"
        entries = [
            {
                "toc_path": toc_path,
                "node_index": 0,
                "raw_href": "ch1.xhtml",
                "title_translated": "第一章译名",
                "title": "第一章",
            }
        ]
        nav = (
            b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            b'<nav epub:type="toc"><ol>'
            b'<li><a href="ch1.xhtml">\xe7\xac\xac\xe4\xb8\x80\xe7\xab\xa0</a></li>'
            b"</ol></nav></body></html>"
        )
        out = _rewrite_toc(nav, entries, is_ncx=False, toc_path=toc_path)
        self.assertIn("第一章译名", out.decode("utf-8"))

        ncx_path = "toc.ncx"
        ncx_entries = [
            {
                "toc_path": ncx_path,
                "node_index": 0,
                "raw_href": "text/ch1.xhtml#x",
                "title_translated": "第一章译名",
                "title": "第一章",
            }
        ]
        ncx = (
            b'<?xml version="1.0"?><ncx><navMap><navPoint>'
            b"<navLabel><text>old</text></navLabel>"
            b'<content src="text/ch1.xhtml#x"/></navPoint></navMap></ncx>'
        )
        out2 = _rewrite_toc(ncx, ncx_entries, is_ncx=True, toc_path=ncx_path)
        dec = out2.decode("utf-8")
        self.assertIn("第一章译名", dec)
        self.assertNotIn(">old<", dec)


class TestEpubTocMisdetectRegression(unittest.TestCase):
    """回归：带「返回目录」链接的正文页不应被当成 TOC 改写。

    对应 #183 / #184：章节标题变成「目录」、目录条目重复 / 悬空 fallback。
    """

    def test_is_nav_rejects_chapter_body_with_content_toc_link(self):
        from trans_novel.assemble.epub_writer import _is_nav

        chapter = (
            b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            b'<section epub:type="chapter">'
            b'<h1><a href="content-toc.xhtml">CHAPTER 1</a></h1>'
            b"<p>Body text.</p></section></body></html>"
        )
        # 旧逻辑只查 epub:type + toc 子串，会把这类正文误判为导航页。
        self.assertFalse(_is_nav(chapter))

    def test_is_nav_accepts_explicit_toc_nav(self):
        from trans_novel.assemble.epub_writer import _is_nav

        nav = (
            b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            b'<nav epub:type="toc"><ol>'
            b'<li><a href="ch1.xhtml">Chapter 1</a></li>'
            b"</ol></nav></body></html>"
        )
        self.assertTrue(_is_nav(nav))
        self.assertTrue(
            _is_nav(b'<html><body><nav role="doc-toc"><ol><li>x</li></ol></nav></body></html>')
        )

    def test_rewrite_toc_skips_chapter_body_without_toc_nav(self):
        from trans_novel.assemble.epub_writer import _rewrite_toc

        chapter = (
            b'<html xmlns:epub="http://www.idpf.org/2007/ops"><body>'
            b'<section epub:type="chapter">'
            b'<h1><a href="content-toc.xhtml">CHAPTER 1</a></h1>'
            b"<p>Body text.</p></section></body></html>"
        )
        entries = [
            {
                "toc_path": "ch1.xhtml",
                "node_index": 0,
                "raw_href": "content-toc.xhtml",
                "title_translated": "目录",
                "title": "Contents",
            }
        ]

        out = _rewrite_toc(chapter, entries, is_ncx=False, toc_path="ch1.xhtml")
        text = out.decode("utf-8")
        self.assertIn("CHAPTER 1", text)
        self.assertNotIn(">目录<", text)
        self.assertEqual(out, chapter)

    def test_heading_wrapped_in_toc_link_keeps_translation_inside_anchor(self):
        """整段源文被 <a href=content-toc> 包住且无对齐时，译文进链接，不挂悬空 ↩。"""
        target = "第一章"
        source = "CHAPTER 1"
        template = (
            '<html><body><h1 data-tn-id="tn1_0">'
            '<a data-tn-annotation-id="ann-0" href="content-toc.xhtml">CHAPTER 1</a>'
            "</h1></body></html>"
        )
        segment = Segment(
            index=0,
            source=source,
            target=target,
            kind="heading",
            anchor="tn1_0",
            meta={
                "epub_annotations": {
                    "version": 1,
                    "source_length": len(source),
                    "items": [
                        {
                            "id": "ann-0",
                            "mode": "range",
                            "source_start": 0,
                            "source_end": len(source),
                            "source_text": source,
                            "marker_text": "",
                        }
                    ],
                    # 故意不给可用 placement / digest；须覆盖整段 source 才走整块回填。
                }
            },
        )

        rendered = BeautifulSoup(
            _render_segments_html(template, [segment]),
            "html.parser",
        )
        heading = rendered.find("h1")
        self.assertIsInstance(heading, Tag)
        assert isinstance(heading, Tag)
        link = heading.find("a")
        self.assertIsInstance(link, Tag)
        assert isinstance(link, Tag)
        self.assertEqual(link.get("href"), "content-toc.xhtml")
        self.assertEqual(link.get_text(strip=True), target)
        self.assertEqual(heading.get_text(strip=True), target)
        self.assertNotIn("↩", heading.get_text())
        self.assertNotIn("目录", heading.get_text())
        self.assertIsNone(rendered.select_one("[data-tn-annotation-id]"))


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
            self.assertNotIn("chapters_reviewed", s)
            self.assertNotIn("review_issues", report)


if __name__ == "__main__":
    unittest.main()
