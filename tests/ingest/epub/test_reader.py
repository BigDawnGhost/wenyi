"""摄取与切分的冒烟测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from tests.fixtures.books import (
    write_cross_resource_toc_epub,
    write_grouped_nav_epub,
    write_nested_toc_epub,
    write_part_chapter_epub,
    write_sample_epub,
)
from trans_novel.epub.archive import safe_name
from trans_novel.epub.slots import slot_contract_digest
from trans_novel.ingest.epub.reader import read_epub
from trans_novel.ingest.segmenter import (
    load_document,
)

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


class TestEpubIngest(unittest.TestCase):
    # ── epub_toc：NCX/NAV 解析与 href 解析 ──────────────────────────────

    def test_missing_rootfile_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "book.epub")
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr(
                    "META-INF/container.xml",
                    "<container><rootfiles><rootfile/></rootfiles></container>",
                )
            with self.assertRaisesRegex(ValueError, "invalid_rootfile"):
                read_epub(path, "en", "zh")

    def test_spine_nav_composes_body_segments_without_toc_links(self):
        """A spine NAV contributes its visible body text, while its TOC list remains immutable."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nav-spine.epub")
            opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
</manifest><spine><itemref idref="nav"/><itemref idref="body"/></spine></package>"""
            nav = """<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops"><head/><body>
<h1>Contents</h1><nav epub:type="toc"><ol>
<li><a href="body.xhtml#one">Chapter One</a></li></ol></nav><p>Nav body.</p>
</body></html>"""
            body = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1 id="one">Chapter One</h1><p>Body.</p></body></html>"""
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
                archive.writestr(
                    "META-INF/container.xml",
                    """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>""",
                )
                archive.writestr("content.opf", opf)
                archive.writestr("nav.xhtml", nav)
                archive.writestr("body.xhtml", body)

            document = load_document(path, "en", "zh")

        self.assertEqual(
            [chapter.href for chapter in document.chapters], ["nav.xhtml", "body.xhtml"]
        )
        self.assertEqual(
            [segment.source for segment in document.chapters[0].segments],
            ["Contents", "Nav body."],
        )
        self.assertEqual(
            [segment.source for segment in document.chapters[1].segments],
            ["Chapter One", "Body."],
        )
        self.assertTrue(
            all(
                segment.epub_state is not None
                for chapter in document.chapters
                for segment in chapter.segments
            )
        )

    def test_standalone_nav_metadata_does_not_create_logical_chapter(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "standalone-nav.epub")
            opf = """<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
</manifest><spine><itemref idref="body"/></spine></package>"""
            nav = """<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops"><body>
<h1>Contents</h1><p>Nav intro.</p><nav epub:type="toc"><ol>
<li><a href="body.xhtml#one">Chapter One</a></li></ol></nav>
</body></html>"""
            body = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1 id="one">Chapter One</h1><p>Body.</p></body></html>"""
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
                archive.writestr(
                    "META-INF/container.xml",
                    """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>""",
                )
                archive.writestr("content.opf", opf)
                archive.writestr("nav.xhtml", nav)
                archive.writestr("body.xhtml", body)

            document = load_document(path, "en", "zh")

        self.assertEqual([chapter.href for chapter in document.chapters], ["body.xhtml"])
        self.assertEqual(document.meta["toc_paths"], ["nav.xhtml"])
        self.assertEqual(document.meta["epub_schema"], 4)
        nav_resource = next(
            resource
            for resource in document.meta["epub_resources"]
            if resource["href"] == "nav.xhtml"
        )
        self.assertEqual(nav_resource["parse_mode"], "xml")

    # ── 8 条边界规则：read_epub / _logical_chapters 端到端与直接单测 ────

    def test_unresolved_fragment_is_not_used_as_a_chapter_boundary(self):
        """规则 1：损坏的 fragment 不得回退到资源开头，否则会在错误位置切章；应丢弃该目录项。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "broken-fragment.epub")
            write_nested_toc_epub(path, broken_part2_fragment=True)

            doc = load_document(path, "en", "zh")

        self.assertEqual([chapter.title for chapter in doc.chapters], ["PART I"])
        broken = next(entry for entry in doc.meta["toc_entries"] if entry["title"] == "PART II")
        self.assertNotIn("segment_anchor", broken)
        self.assertNotIn("boundary_position", broken)

    def test_epub_keeps_toc_entry_for_skipped_title_page(self):
        """规则 3：无字标题页也是有效边界（边界=资源起点），标题取自 TOC。"""
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
        self.assertEqual(doc.chapters[0].title, "第一章")
        self.assertTrue(
            any(
                entry.get("resource_href") == "title.xhtml" and entry.get("title") == "第一章"
                for entry in doc.meta["toc_entries"]
            )
        )

    def test_real_boundary_wins_when_empty_title_page_has_same_position(self):
        """规则 3（续）：空标题页与下一真实章边界重叠时，真实章边界胜出。"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "empty-title.epub")
            write_nested_toc_epub(path, empty_title_page=True)

            document = load_document(path, "en", "zh")

        self.assertEqual([chapter.title for chapter in document.chapters], ["PART I", "PART II"])
        title_page, first_part = document.meta["toc_entries"][:2]
        self.assertEqual(title_page["boundary_position"], 0)
        self.assertNotIn("segment_anchor", title_page)
        self.assertEqual(first_part["boundary_position"], 0)
        self.assertTrue(first_part.get("segment_anchor"))

    def test_unlinked_top_level_nav_groups_inherit_first_child_boundary(self):
        """规则 4：无 href 的分组节点继承第一个可定位子节点的边界。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grouped.epub")
            write_grouped_nav_epub(path)

            doc = load_document(path, "en", "zh")

        self.assertEqual([chapter.title for chapter in doc.chapters], ["PART I", "PART II"])
        self.assertEqual(
            [segment.source for segment in doc.chapters[0].segments],
            ["Section 1", "One."],
        )
        self.assertEqual(
            [segment.source for segment in doc.chapters[1].segments],
            ["Section 2", "Two."],
        )
        group_entries = [entry for entry in doc.meta["toc_entries"] if entry["depth"] == 0]
        self.assertTrue(all("inherited_boundary_from" in entry for entry in group_entries))

    def test_preface_before_first_boundary_becomes_separate_chapter(self):
        """规则 5：首个目录边界前仍有正文 → 独立前置章，标题取首个 heading。"""
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
<item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine toc="ncx"><itemref idref="body"/></spine>
</package>""",
                )
                zf.writestr(
                    "toc.ncx",
                    """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
<navMap><navPoint id="n1"><navLabel><text>Chapter One</text></navLabel>
<content src="body.xhtml#ch1"/></navPoint></navMap>
</ncx>""",
                )
                zf.writestr(
                    "body.xhtml",
                    """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h2>Prologue</h2><p>Intro text.</p>
<h1 id="ch1">Chapter One</h1><p>Body one.</p>
</body></html>""",
                )

            doc = load_document(p, "en", "zh")

        self.assertEqual([c.title for c in doc.chapters], ["Prologue", "Chapter One"])
        preface, first = doc.chapters
        self.assertNotIn("toc_entry_id", preface.meta)
        self.assertEqual([s.source for s in preface.segments], ["Prologue", "Intro text."])
        self.assertEqual(first.meta.get("toc_entry_id"), "toc.ncx:0")

    def test_epub_chapters_and_anchors(self):
        """规则 6：无任何可用目录边界 → spine-fallback，每个非空资源一章。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "novel.epub")
            write_sample_epub(p)
            doc = load_document(p, "ja", "zh")

        self.assertEqual(doc.fmt, "epub")
        self.assertEqual(doc.meta["epub_split_strategy"], "spine-fallback")
        self.assertEqual(len(doc.chapters), 2)
        ch1 = doc.chapters[0]
        self.assertEqual(ch1.title, "第一章　出会い")
        self.assertEqual(len(ch1.text_segments), 3)  # h1 + 2 p
        self.assertNotIn("toc_entry_id", ch1.meta)
        self.assertEqual(doc.meta["epub_schema"], 4)
        self.assertNotIn("epub_resource_templates", doc.meta)
        self.assertIsNotNone(ch1.href)
        for s in ch1.text_segments:
            state = s.epub_state
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.resource_href, ch1.href)
            self.assertEqual(state.parse_mode, "xml")
            self.assertTrue(state.resource_sha256)
            self.assertTrue(state.block_fingerprint)
            self.assertEqual(state.slot_contract_sha256, slot_contract_digest(state.slots))

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


class TestEpubNavigationSelection(unittest.TestCase):
    def test_nav_is_canonical_when_epub_also_contains_legacy_ncx(self):
        """规则 7：多份目录同时存在时，选择排序最靠前且能产出边界的目录（NAV）。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "dual-toc.epub")
            write_nested_toc_epub(path, toc_kind="both")

            doc = load_document(path, "en", "zh")

        self.assertEqual([chapter.title for chapter in doc.chapters], ["PART I", "PART II"])
        self.assertEqual(len(doc.meta["toc_entries"]), 8)
        self.assertEqual(doc.meta["epub_split_toc_path"], "OEBPS/nav.xhtml")

    def test_toc_falls_back_to_next_toc_when_primary_yields_no_boundaries(self):
        """规则 7（续）：首选目录无法产出边界（如条目均为外部链接）时，改用下一份目录。"""
        nav = """<html xmlns="http://www.w3.org/1999/xhtml"
        xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>
        <li><a href="https://example.com/external">External Only</a></li>
        </ol></nav></body></html>"""
        ncx = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
<navMap><navPoint><navLabel><text>Chapter One</text></navLabel>
<content src="body.xhtml"/></navPoint></navMap>
</ncx>"""
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine><itemref idref="body"/></spine>
</package>"""
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
                zf.writestr("content.opf", opf)
                zf.writestr("nav.xhtml", nav)
                zf.writestr("toc.ncx", ncx)
                zf.writestr(
                    "body.xhtml",
                    "<html><body><h1>Chapter One</h1><p>Body.</p></body></html>",
                )

            doc = load_document(p, "en", "zh")

        self.assertEqual([c.title for c in doc.chapters], ["Chapter One"])
        self.assertEqual(doc.meta["epub_split_toc_path"], "toc.ncx")

    def test_logical_chapter_can_span_multiple_spine_resources(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cross.epub")
            write_cross_resource_toc_epub(path)

            doc = load_document(path, "en", "zh")

        self.assertEqual([chapter.title for chapter in doc.chapters], ["PART I", "PART II"])
        self.assertEqual(
            [segment.source for segment in doc.chapters[0].segments],
            ["PART I", "One.", "Section 1", "Two."],
        )
        self.assertEqual(
            {segment.resource_href for segment in doc.chapters[0].segments},
            {"OEBPS/one.xhtml", "OEBPS/two.xhtml"},
        )
        self.assertEqual(
            [segment.source for segment in doc.chapters[1].segments],
            ["PART II", "Three."],
        )

    def test_nested_toc_splits_only_top_level_and_keeps_all_anchors(self):
        expected = [
            ("PART I", 0, "part-1"),
            ("Section 1", 1, "section-1"),
            ("PART II", 0, "part-2"),
            ("Section 2", 1, "section-2"),
        ]
        for toc_kind in ("ncx", "nav"):
            with self.subTest(toc_kind=toc_kind), tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "nested.epub")
                write_nested_toc_epub(path, toc_kind=toc_kind)

                doc = load_document(path, "en", "zh")

                self.assertEqual([chapter.title for chapter in doc.chapters], ["PART I", "PART II"])
                self.assertEqual(
                    [segment.source for segment in doc.chapters[0].segments],
                    ["PART I", "Part I intro.", "Section 1", "Section 1 body."],
                )
                self.assertEqual(
                    [segment.source for segment in doc.chapters[1].segments],
                    ["PART II", "Part II intro.", "Section 2", "Section 2 body."],
                )
                self.assertEqual(
                    [
                        (entry["title"], entry["depth"], entry["fragment"])
                        for entry in doc.meta["toc_entries"]
                    ],
                    expected,
                )
                self.assertEqual(
                    {entry["resource_href"] for entry in doc.meta["toc_entries"]},
                    {"OEBPS/body.xhtml"},
                )
                self.assertTrue(
                    all(
                        segment.resource_href == "OEBPS/body.xhtml"
                        for chapter in doc.chapters
                        for segment in chapter.segments
                    )
                )

    def test_part_chapter_toc_selects_chapter_depth_when_chapter_slices_are_large(self):
        """select_boundaries：章级（depth 1）切片的字符数中位数达标时，选择更细的粒度。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "part_chapter_large.epub")
            write_part_chapter_epub(path, chapter_body_chars=3500)

            doc = load_document(path, "en", "zh")

        self.assertEqual(doc.meta["epub_split_strategy"], "toc-depth-1")
        self.assertEqual(
            [chapter.title for chapter in doc.chapters],
            ["第1部", "第1章", "第2章", "第3章", "第2部", "第4章", "第5章", "第6章"],
        )

    def test_part_chapter_toc_falls_back_to_part_depth_when_chapter_slices_are_too_small(self):
        """select_boundaries：章级（depth 1）切片的字符数中位数低于 3000 时，退回部级（depth 0）粒度。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "part_chapter_small.epub")
            write_part_chapter_epub(path, chapter_body_chars=1200)

            doc = load_document(path, "en", "zh")

        self.assertEqual(doc.meta["epub_split_strategy"], "toc-depth-0")
        self.assertEqual([chapter.title for chapter in doc.chapters], ["第1部", "第2部"])

    def test_manifest_only_recovered_xhtml_persists_resource_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "manifest-only.epub")
            files = {
                "mimetype": b"application/epub+zip",
                "META-INF/container.xml": (
                    b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    b'<rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>'
                ),
                "OEBPS/book.opf": (
                    b'<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                    b'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
                    b"<dc:language>en</dc:language></metadata><manifest>"
                    b'<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
                    b'<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>'
                    b'<item id="strict" href="strict.xhtml" media-type="application/xhtml+xml"/>'
                    b'</manifest><spine><itemref idref="chapter"/></spine></package>'
                ),
                "OEBPS/chapter.xhtml": b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Text</p></body></html>',
                "OEBPS/notes.xhtml": b"<html><body><p>Auxiliary notes",
                "OEBPS/strict.xhtml": b'<html xmlns="http://www.w3.org/1999/xhtml" lang="en"><body><p>Auxiliary</p></body></html>',
            }
            with zipfile.ZipFile(path, "w") as archive:
                for name, data in files.items():
                    archive.writestr(
                        name,
                        data,
                        zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED,
                    )
            document = load_document(path, "en", "zh")
            resources = {item["href"]: item for item in document.meta["epub_resources"]}
            self.assertEqual(resources["OEBPS/notes.xhtml"]["parse_mode"], "recovered")
            self.assertEqual(resources["OEBPS/strict.xhtml"]["parse_mode"], "xml")
            self.assertTrue(
                all(
                    segment.resource_href != "OEBPS/notes.xhtml"
                    for chapter in document.chapters
                    for segment in chapter.segments
                )
            )

    def test_unsafe_canonical_alias_rejected_before_epub_parse(self):
        self.assertFalse(safe_name("META-INF/x/../container.xml"))
        self.assertFalse(safe_name("META-INF//container.xml"))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "unsafe.epub")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("META-INF/x/../container.xml", b"<container/>")
            with self.assertRaisesRegex(ValueError, "unsafe_entry"):
                read_epub(path, "en", "zh")


if __name__ == "__main__":
    unittest.main()
