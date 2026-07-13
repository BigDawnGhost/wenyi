"""摄取与切分的冒烟测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from trans_novel.ingest.segmenter import (
    load_document, chapter_batches, split_long_segments, _split_text)
from trans_novel.ingest.models import KIND_HEADING, KIND_TEXT, Chapter, Segment
from tests.sample_data import write_sample_txt, write_sample_epub


class TestTextIngest(unittest.TestCase):
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
