"""摄取与切分的冒烟测试。"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
import zipfile

from trans_novel.ingest.segmenter import (
    load_document, chapter_batches, split_long_segments, _split_text)
from trans_novel.ingest.models import KIND_HEADING, KIND_TEXT, Chapter, Segment
from trans_novel.ingest.epub_reader import (
    _decode_markup,
    _extract_chapter,
    _find_opf_path,
    _parse_opf,
)
from trans_novel.ingest.fb2_reader import read_fb2_binaries
from tests.sample_data import write_sample_txt, write_sample_epub


class TestTextIngest(unittest.TestCase):
    def test_untitled_preface_does_not_gain_book_title_heading(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "book.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("preface\n\n# Chapter 1\nbody\n\n# Chapter 2\nbody")

            doc = load_document(p, "en", "zh")

        self.assertEqual(doc.chapters[0].title, "book")
        self.assertEqual(
            [(segment.kind, segment.source) for segment in doc.chapters[0].segments],
            [(KIND_TEXT, "preface")],
        )

    def test_text_chapters_and_segments(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.txt")
            write_sample_txt(p)
            doc = load_document(p, "ja", "zh")

        self.assertEqual(doc.fmt, "text")
        self.assertEqual(len(doc.chapters), 2)
        ch1 = doc.chapters[0]
        self.assertEqual(ch1.title, "第一章　出会い")
        # 标题 heading + 3 段正文
        self.assertEqual(ch1.segments[0].kind, KIND_HEADING)
        self.assertEqual(len(ch1.text_segments), 4)

    def test_preamble_before_first_heading_does_not_gain_book_title(self):
        content = "这是前言。\n\n# 第一章\n\n这是正文。\n"
        for suffix in (".txt", ".md"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "novel" + suffix)
                with open(path, "w", encoding="utf-8") as file:
                    file.write(content)

                document = load_document(path, "zh", "en")

                self.assertEqual(len(document.chapters), 2)
                self.assertEqual(
                    [segment.kind for segment in document.chapters[0].segments],
                    [KIND_TEXT],
                )
                self.assertEqual(document.chapters[0].segments[0].source, "这是前言。")
                self.assertEqual(document.chapters[1].segments[0].kind, KIND_HEADING)
                self.assertEqual(document.chapters[1].segments[0].source, "第一章")

    def test_batching(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.txt")
            write_sample_txt(p)
            doc = load_document(p, "ja", "zh")
        batches = chapter_batches(doc.chapters[0], max_chars=60)
        # 总段数守恒
        total = sum(len(b) for b in batches)
        self.assertEqual(total, len(doc.chapters[0].text_segments))
        self.assertGreater(len(batches), 1)  # 60 字符预算应切出多批


_FB2_FLAT = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
<description><title-info><book-title>平铺之书</book-title></title-info></description>
<body>
  <section><title><p>第一章</p></title><p>第一段。</p><p>第二段。</p></section>
  <section><title><p>第二章</p></title><p>仅一段。</p></section>
</body>
<body name="notes"><section><p>这是注释，应被跳过。</p></section></body>
</FictionBook>
"""

_FB2_BODY_TITLE = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
<description><title-info><book-title>正文标题之书</book-title></title-info></description>
<body>
  <title><p>作者姓名</p><p>正文标题之书</p></title>
  <section><title><p>第一章</p></title><p>第一段。</p></section>
</body>
</FictionBook>
"""

# 嵌套：部 → 章（section 套 section）。容器节正文不得丢失。
_FB2_NESTED = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
<description><title-info><book-title>嵌套之书</book-title></title-info></description>
<body>
  <section>
    <title><p>第一部</p></title>
    <section><title><p>第一章</p></title><p>一章首段。</p><p>一章次段。</p></section>
    <section><title><p>第二章</p></title><p>二章仅一段。</p></section>
  </section>
</body>
</FictionBook>
"""


# subtitle / poem / cite / text-author 等正文块不得丢字
_FB2_BLOCKS = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
<description><title-info><book-title>块之书</book-title></title-info></description>
<body>
  <section>
    <title><p>第一章</p></title>
    <epigraph><p>题记一行。</p><text-author>题记作者</text-author></epigraph>
    <p>普通段落。</p>
    <subtitle>场景小标题</subtitle>
    <poem><title><p>诗名</p></title>
      <stanza><v>第一诗行。</v><v>第二诗行。</v></stanza>
      <text-author>诗人</text-author></poem>
    <cite><p>引文段落。</p><text-author>引文作者</text-author></cite>
    <p>结尾段落。</p>
  </section>
</body>
</FictionBook>
"""

_FB2_IMAGES = """\
<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
             xmlns:xlink="http://www.w3.org/1999/xlink">
<description><title-info>
  <book-title>插图之书</book-title>
  <coverpage><image xlink:href="#cover.jpg"/></coverpage>
</title-info></description>
<body>
  <section><title><p>第一章</p></title>
    <image xlink:href="#inside.png"/>
    <p>带插图的正文。</p>
  </section>
</body>
<binary id="cover.jpg" content-type="image/jpeg">Y292ZXItYnl0ZXM=</binary>
<binary id="inside.png" content-type="image/png">aW5zaWRlLWJ5dGVz</binary>
</FictionBook>
"""


class TestFb2Ingest(unittest.TestCase):
    def _load(self, content: str):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.fb2")
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            return load_document(p, "ja", "zh")

    def test_flat_sections_and_notes_skipped(self):
        doc = self._load(_FB2_FLAT)
        self.assertEqual(doc.fmt, "fb2")
        self.assertEqual(doc.title, "平铺之书")
        self.assertEqual(len(doc.chapters), 2)  # notes body 不计入
        ch1 = doc.chapters[0]
        self.assertEqual(ch1.title, "第一章")
        self.assertEqual(ch1.segments[0].kind, KIND_HEADING)
        self.assertEqual(len(ch1.text_segments), 3)  # 标题 + 2 段
        # 注释正文不应出现在任何章中
        all_src = [s.source for ch in doc.chapters for s in ch.segments]
        self.assertNotIn("这是注释，应被跳过。", all_src)

    def test_namespace_variants_are_supported(self):
        variants = {
            "2.1": _FB2_FLAT.replace("fictionbook/2.0", "fictionbook/2.1"),
            "none": _FB2_FLAT.replace(
                ' xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"', ""
            ),
        }
        for name, content in variants.items():
            with self.subTest(namespace=name):
                doc = self._load(content)
                self.assertEqual(doc.title, "平铺之书")
                self.assertEqual([ch.title for ch in doc.chapters], ["第一章", "第二章"])
                self.assertEqual(
                    [s.source for s in doc.chapters[0].text_segments],
                    ["第一章", "第一段。", "第二段。"],
                )

    def test_single_quoted_windows_1251_declaration(self):
        content = """<?xml version='1.0' encoding='windows-1251'?>
<FictionBook>
  <description><title-info><book-title>Детство</book-title></title-info></description>
  <body><section><title><p>Глава</p></title><p>Текст</p></section></body>
</FictionBook>"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "book.fb2")
            with open(path, "wb") as f:
                f.write(content.encode("windows-1251"))

            doc = load_document(path, "ru", "zh")

        self.assertEqual(doc.title, "Детство")
        self.assertEqual(doc.chapters[0].segments[0].source, "Глава")

    def test_body_title_becomes_a_separate_chapter(self):
        doc = self._load(_FB2_BODY_TITLE)
        self.assertEqual(len(doc.chapters), 2)
        title_page, first_chapter = doc.chapters
        self.assertEqual(title_page.title, "正文标题之书")
        self.assertEqual(
            [s.source for s in title_page.segments],
            ["作者姓名", "正文标题之书"],
        )
        self.assertTrue(all(s.kind == KIND_HEADING for s in title_page.segments))
        self.assertEqual([s.index for s in title_page.segments], [0, 1])
        self.assertEqual(
            [s.anchor for s in title_page.segments],
            ["tn0_0", "tn0_1"],
        )
        self.assertEqual(first_chapter.index, 1)
        self.assertEqual(first_chapter.title, "第一章")
        self.assertEqual(
            [s.anchor for s in first_chapter.segments],
            ["tn1_0", "tn1_1"],
        )

    def test_block_types_not_lost(self):
        doc = self._load(_FB2_BLOCKS)
        ch = doc.chapters[0]
        texts = [s.source for s in ch.segments]
        for expect in ["题记一行。", "题记作者", "普通段落。", "诗名",
                       "第一诗行。", "第二诗行。", "诗人", "引文段落。",
                       "引文作者", "结尾段落。"]:
            self.assertIn(expect, texts)
        # subtitle 作为 heading
        headings = [s.source for s in ch.segments if s.kind == KIND_HEADING]
        self.assertIn("场景小标题", headings)

    def test_nested_sections_not_lost(self):
        doc = self._load(_FB2_NESTED)
        # 部标题成一章 + 两个子章，正文一段不丢
        titles = [ch.title for ch in doc.chapters]
        self.assertEqual(titles, ["第一部", "第一章", "第二章"])
        all_text = [s.source for ch in doc.chapters
                    for s in ch.text_segments if s.kind != KIND_HEADING]
        self.assertIn("一章首段。", all_text)
        self.assertIn("一章次段。", all_text)
        self.assertIn("二章仅一段。", all_text)

    def test_images_and_cover_are_recorded_without_persisting_binary_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "images.fb2")
            with open(path, "w", encoding="utf-8") as file:
                file.write(_FB2_IMAGES)

            doc = load_document(path, "ru", "zh")
            binaries = read_fb2_binaries(path)

        self.assertEqual(doc.meta["fb2_cover_image"], "cover.jpg")
        self.assertEqual(
            doc.meta["fb2_resources"],
            [
                {"id": "cover.jpg", "content_type": "image/jpeg"},
                {"id": "inside.png", "content_type": "image/png"},
            ],
        )
        self.assertEqual(
            doc.chapters[0].meta["fb2_images"],
            [{"id": "inside.png", "position": 1}],
        )
        self.assertNotIn(base64.b64encode(b"cover-bytes").decode(), str(doc.meta))
        self.assertEqual(binaries["cover.jpg"], ("image/jpeg", b"cover-bytes"))
        self.assertEqual(binaries["inside.png"], ("image/png", b"inside-bytes"))


class TestSplitLongSegments(unittest.TestCase):
    def test_split_by_sentence_and_cont_flag(self):
        long_src = "第一句。" * 10            # 40 字符
        ch = Chapter(index=0, title="章", segments=[
            Segment(index=0, source="标题", kind=KIND_HEADING, anchor="a0"),
            Segment(index=1, source=long_src, kind=KIND_TEXT, anchor="a1"),
            Segment(index=2, source="短。", kind=KIND_TEXT, anchor="a2"),
        ])
        split_long_segments([ch], max_chars=30)
        # 长段被拆成多段：首段保留 anchor，续段 cont=True 且无 anchor
        conts = [s.cont for s in ch.segments]
        self.assertIn(True, conts)
        long_parts = [s for s in ch.segments if not s.cont and s.anchor == "a1"]
        self.assertEqual(len(long_parts), 1)            # 首段唯一带 a1
        cont_parts = [s for s in ch.segments if s.cont]
        self.assertTrue(all(s.anchor is None for s in cont_parts))
        # index 连续重排
        self.assertEqual([s.index for s in ch.segments], list(range(len(ch.segments))))
        # 拼回去等于原文
        joined = "".join(s.source for s in ch.segments if s.anchor == "a1" or s.cont)
        self.assertEqual(joined, long_src)

    def test_split_keeps_meta_only_on_anchored_first_part(self):
        original = Segment(
            index=0,
            source="第一句。" * 10,
            kind=KIND_TEXT,
            anchor="a0",
            meta={
                "epub_inline": {
                    "version": 1,
                    "source_length": 40,
                    "nodes": [{"id": "a0_inline_0", "offset": 0}],
                }
            },
        )
        ch = Chapter(index=0, segments=[original])

        split_long_segments([ch], max_chars=20)

        self.assertGreater(len(ch.segments), 1)
        self.assertEqual(ch.segments[0].meta, original.meta)
        self.assertIsNot(ch.segments[0].meta, original.meta)
        self.assertTrue(all(not segment.meta for segment in ch.segments[1:]))

    def test_no_split_when_short(self):
        ch = Chapter(index=0, title="章", segments=[
            Segment(index=0, source="短句。", kind=KIND_TEXT, anchor="a0")])
        split_long_segments([ch], max_chars=100)
        self.assertEqual(len(ch.segments), 1)
        self.assertFalse(ch.segments[0].cont)

    def test_oversized_single_sentence_hard_split(self):
        chunks = _split_text("あ" * 50, 20)   # 无句末标点的超长串
        self.assertTrue(all(len(c) <= 20 for c in chunks))
        self.assertEqual("".join(chunks), "あ" * 50)

    def test_english_splits_on_sentence_punctuation(self):
        text = "Alpha beta gamma. Delta epsilon zeta! Eta theta iota?"
        chunks = _split_text(text, 25)
        self.assertEqual(chunks, ["Alpha beta gamma.", " Delta epsilon zeta!", " Eta theta iota?"])
        self.assertEqual("".join(chunks), text)

    def test_oversized_english_sentence_does_not_split_words(self):
        text = "alphabet bravo charlie delta"
        chunks = _split_text(text, 18)
        self.assertEqual(chunks, ["alphabet bravo", " charlie delta"])
        self.assertEqual("".join(chunks), text)
        self.assertNotIn("char", chunks[0])
        self.assertEqual(chunks[1].split()[0], "charlie")


class TestEpubIngest(unittest.TestCase):
    def test_ruby_reading_is_not_included_in_translatable_source(self):
        html = """<html><body>
<p><ruby>漢字<rt>かんじ</rt></ruby>です</p>
</body></html>"""

        _title, segments, template = _extract_chapter(html, 0, "chapter.xhtml")

        self.assertEqual([segment.source for segment in segments], ["漢字です"])
        self.assertIn("<rt>かんじ</rt>", template)

    def test_table_and_definition_list_cells_are_extracted(self):
        html = """<html><body>
<table><tr><td>Cell A</td><td>Cell B</td></tr></table>
<dl><dt>Term</dt><dd>Definition</dd></dl>
</body></html>"""

        _title, segments, _template = _extract_chapter(html, 0, "chapter.xhtml")

        self.assertEqual(
            [segment.source for segment in segments],
            ["Cell A", "Cell B", "Term", "Definition"],
        )

    def test_declared_legacy_xhtml_encoding_is_honored(self):
        markup = (
            '<?xml version="1.0" encoding="Shift_JIS"?>'
            "<html><body><p>日本語</p></body></html>"
        ).encode("shift_jis")

        decoded = _decode_markup(markup)

        self.assertIn("日本語", decoded)
        self.assertNotIn("�", decoded)

    def test_missing_required_opf_attributes_are_reported_or_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "book.epub")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr(
                    "META-INF/container.xml",
                    "<container><rootfiles><rootfile/></rootfiles></container>",
                )
            with zipfile.ZipFile(path) as zf:
                with self.assertRaisesRegex(ValueError, "full-path"):
                    _find_opf_path(zf)

            opf_path = os.path.join(d, "opf.epub")
            with zipfile.ZipFile(opf_path, "w") as zf:
                zf.writestr(
                    "content.opf",
                    """<package><manifest>
<item href="ignored.xhtml" media-type="application/xhtml+xml"/>
<item id="valid" href="valid.xhtml" media-type="application/xhtml+xml"/>
</manifest><spine><itemref/><itemref idref="valid"/></spine></package>""",
                )
            with zipfile.ZipFile(opf_path) as zf:
                _title, hrefs, _toc = _parse_opf(zf, "content.opf")
            self.assertEqual(hrefs, ["valid.xhtml"])

    def test_epub_records_inline_nodes_in_segment_meta(self):
        html = """<html><body>
<p class="Textbody"><img src="before.jpg"/>Avant<br/>Après<img src="after.jpg"/></p>
<p class="illustration"><img src="standalone.jpg"/></p>
</body></html>"""

        _title, segments, template = _extract_chapter(
            html,
            2,
            "chapter.xhtml",
        )

        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertEqual(segment.source, "AvantAprès")
        inline = segment.meta["epub_inline"]
        self.assertEqual(inline["source_length"], len(segment.source))
        self.assertEqual(
            [node["placement"] for node in inline["nodes"]],
            ["before", "inline", "after"],
        )
        self.assertEqual(
            [node["offset"] for node in inline["nodes"]],
            [0, len("Avant"), len(segment.source)],
        )
        self.assertEqual(template.count("data-tn-inline-id"), 3)
        self.assertIn('<img src="standalone.jpg"/>', template)

    def test_epub_chapters_and_anchors(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.epub")
            write_sample_epub(p)
            doc = load_document(p, "ja", "zh")

        self.assertEqual(doc.fmt, "epub")
        self.assertEqual(len(doc.chapters), 2)
        ch1 = doc.chapters[0]
        self.assertEqual(ch1.title, "第一章　出会い")
        self.assertEqual(len(ch1.text_segments), 3)  # h1 + 2 p
        # 每个 segment 都有回填锚点，且模板里含该锚点
        template = ch1.template
        self.assertIsNotNone(template)
        assert template is not None
        for s in ch1.text_segments:
            anchor = s.anchor
            self.assertIsNotNone(anchor)
            assert anchor is not None
            self.assertIn(anchor, template)
        self.assertIsNotNone(ch1.href)

    def test_epub_ignores_internal_file_title_when_no_heading(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.epub")
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
                zf.writestr(
                    "META-INF/container.xml",
                    """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>
</container>""",
                )
                zf.writestr(
                    "OEBPS/content.opf",
                    """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title></metadata>
<manifest><item id="cUH.xhtml" href="cUH.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="cUH.xhtml"/></spine>
</package>""",
                )
                zf.writestr(
                    "OEBPS/cUH.xhtml",
                    """<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>cUH</title></head><body><p>Body text.</p></body>
</html>""",
                )

            doc = load_document(p, "en", "zh")

        self.assertEqual(len(doc.chapters), 1)
        self.assertEqual(doc.chapters[0].title, "")

    def test_epub_uses_ncx_toc_label_before_repeated_html_title(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.epub")
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
                zf.writestr(
                    "META-INF/container.xml",
                    """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="content.opf"/></rootfiles>
</container>""",
                )
                zf.writestr(
                    "content.opf",
                    """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Intermezzo</dc:title></metadata>
<manifest>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="ch1" href="index_split_004.html" media-type="application/xhtml+xml"/>
</manifest>
<spine toc="ncx"><itemref idref="ch1"/></spine>
</package>""",
                )
                zf.writestr(
                    "toc.ncx",
                    """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
<navMap><navPoint id="n1" playOrder="1">
<navLabel><text>Chapter 1</text></navLabel>
<content src="index_split_004.html"/>
</navPoint></navMap>
</ncx>""",
                )
                zf.writestr(
                    "index_split_004.html",
                    """<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Intermezzo</title></head><body><p>1</p><p>Body text.</p></body>
</html>""",
                )

            doc = load_document(p, "en", "zh")

        self.assertEqual(len(doc.chapters), 1)
        self.assertEqual(doc.chapters[0].title, "Chapter 1")

    def test_epub_keeps_toc_entry_for_skipped_title_page(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.epub")
            with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
                zf.writestr(
                    "META-INF/container.xml",
                    """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="content.opf"/></rootfiles>
</container>""",
                )
                zf.writestr(
                    "content.opf",
                    """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title></metadata>
<manifest>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>
<item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine toc="ncx"><itemref idref="title"/><itemref idref="body"/></spine>
</package>""",
                )
                zf.writestr(
                    "toc.ncx",
                    """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
<navMap><navPoint id="n1" playOrder="1">
<navLabel><text>第一章</text></navLabel><content src="title.xhtml"/>
</navPoint></navMap>
</ncx>""",
                )
                zf.writestr(
                    "title.xhtml",
                    """<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="title.jpg"/></body></html>""",
                )
                zf.writestr(
                    "body.xhtml",
                    """<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Body text.</p></body></html>""",
                )

            doc = load_document(p, "ja", "zh")

        self.assertEqual(len(doc.chapters), 1)
        self.assertEqual(doc.chapters[0].href, "body.xhtml")
        self.assertEqual(doc.chapters[0].title, "")
        self.assertIn({"href": "title.xhtml", "title": "第一章"}, doc.meta["toc_entries"])


if __name__ == "__main__":
    unittest.main()
