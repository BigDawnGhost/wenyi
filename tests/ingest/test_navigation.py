"""摄取与切分的冒烟测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile

from tests.fixtures.books import (
    write_nested_toc_epub,
)
from trans_novel.epub.navigation import parse_toc_entries, resolve_epub_href
from trans_novel.ingest import Chapter, ChapterProcessing, Segment, preserved_toc_entry_ids
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

    def test_nav_without_epub_type_uses_first_navigation_list(self):
        nav = """<html><body><nav><h1>Contents</h1><ol>
        <li><a href="body.xhtml#one">One</a></li>
        </ol></nav></body></html>"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "toc.zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("OEBPS/nav.xhtml", nav)
            with zipfile.ZipFile(path) as archive:
                entries = parse_toc_entries(archive, {"OEBPS/nav.xhtml": "nav"})

        self.assertEqual([entry["title"] for entry in entries], ["One"])
        self.assertEqual(entries[0]["resource_href"], "OEBPS/body.xhtml")

    def test_broken_secondary_toc_does_not_block_valid_primary_nav(self):
        nav = """<html><body><nav epub:type="toc"><ol>
        <li><a href="body.xhtml#one">One</a></li>
        </ol></nav></body></html>"""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "toc.zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("OEBPS/nav.xhtml", nav)
                archive.writestr("OEBPS/toc.ncx", "<ncx><navMap>")
            with zipfile.ZipFile(path) as archive:
                entries = parse_toc_entries(
                    archive, {"OEBPS/nav.xhtml": "nav", "OEBPS/toc.ncx": "ncx"}
                )

        self.assertEqual([entry["title"] for entry in entries], ["One"])

    def test_ncx_with_xml_extension_uses_manifest_media_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "toc-xml.epub")
            write_nested_toc_epub(path, ncx_filename="toc.xml")

            document = load_document(path, "en", "zh")

        self.assertEqual([chapter.title for chapter in document.chapters], ["PART I", "PART II"])
        self.assertEqual(document.meta["toc_paths"], ["OEBPS/toc.xml"])
        self.assertTrue(all(entry["kind"] == "ncx" for entry in document.meta["toc_entries"]))

    def test_epub_href_resolution_preserves_raw_href_and_plus(self):
        resolved = resolve_epub_href(
            "OEBPS/nav/toc.xhtml",
            "../text/A+B%20C.xhtml#section%201",
        )

        self.assertEqual(resolved.raw_href, "../text/A+B%20C.xhtml#section%201")
        self.assertEqual(resolved.resource_href, "OEBPS/text/A+B C.xhtml")
        self.assertEqual(resolved.fragment, "section 1")
        self.assertEqual(resolved.target_key, "OEBPS/text/A+B C.xhtml#section 1")

    def test_preserved_toc_ids_cover_all_navigation_surfaces_and_safe_groups(self):
        preserved = ChapterProcessing(
            action="preserve",
            review_required=False,
            reason="references",
            source_sha256="source",
            strategy_version="chapter_semantics_v1",
        )
        chapters = [
            Chapter(
                index=0,
                segments=[
                    Segment(index=0, source="A", resource_href="text.xhtml", anchor="a"),
                    Segment(index=1, source="B", resource_href="text.xhtml", anchor="b"),
                ],
                processing=preserved,
                meta={"toc_entry_id": "legacy"},
            ),
            Chapter(
                index=1,
                segments=[Segment(index=0, source="C", resource_href="text.xhtml", anchor="c")],
            ),
        ]
        entries = [
            {
                "entry_id": "nav-a",
                "toc_path": "nav",
                "node_index": 0,
                "raw_href": "text.xhtml#a",
                "boundary_position": 0,
            },
            {
                "entry_id": "nav-b",
                "toc_path": "nav",
                "node_index": 1,
                "parent_index": 0,
                "raw_href": "text.xhtml#b",
                "boundary_position": 1,
            },
            {
                "entry_id": "ncx-a",
                "toc_path": "ncx",
                "node_index": 0,
                "raw_href": "text.xhtml#a",
                "segment_anchor": "a",
                "resource_href": "text.xhtml",
            },
            {
                "entry_id": "normal",
                "toc_path": "nav",
                "node_index": 2,
                "raw_href": "text.xhtml#c",
                "boundary_position": 2,
            },
            {"entry_id": "preserved-group", "toc_path": "groups", "node_index": 0, "raw_href": ""},
            {
                "entry_id": "preserved-child",
                "toc_path": "groups",
                "node_index": 1,
                "parent_index": 0,
                "raw_href": "text.xhtml#b",
                "boundary_position": 1,
            },
            {"entry_id": "mixed-group", "toc_path": "groups", "node_index": 2, "raw_href": ""},
            {
                "entry_id": "mixed-preserved",
                "toc_path": "groups",
                "node_index": 3,
                "parent_index": 2,
                "raw_href": "text.xhtml#b",
                "boundary_position": 1,
            },
            {
                "entry_id": "mixed-normal",
                "toc_path": "groups",
                "node_index": 4,
                "parent_index": 2,
                "raw_href": "text.xhtml#c",
                "boundary_position": 2,
            },
            {
                "entry_id": "external",
                "raw_href": "https://example.test",
                "boundary_position": 0,
                "external": True,
            },
            {"entry_id": "legacy", "raw_href": "missing.xhtml"},
        ]

        self.assertEqual(
            preserved_toc_entry_ids(chapters, entries),
            {
                "nav-a",
                "nav-b",
                "ncx-a",
                "preserved-group",
                "preserved-child",
                "mixed-preserved",
                "legacy",
            },
        )

    # ── schema-4 EPUB extraction and chapter-boundary behavior ─────────────


if __name__ == "__main__":
    unittest.main()
