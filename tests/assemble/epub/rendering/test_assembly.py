"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from bs4 import BeautifulSoup, Tag

from tests.fixtures.books import (
    write_cross_resource_toc_epub,
    write_epub_type_less_nav_epub,
    write_inline_sample_epub,
    write_nested_toc_epub,
    write_phase9_epub,
    write_sample_epub,
)
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble import assemble
from trans_novel.assemble.epub.verification import validate_epub_triplet
from trans_novel.config import Config
from trans_novel.epub.slots import distribute_slot_translation
from trans_novel.ingest.models import Chapter
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application
from trans_novel.pipeline.state import RunStore
from trans_novel.postprocess.punct import normalize_heading_numbering

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
    config = Config.from_dict({"llm": fake_llm_dict(), "quality": "quality"})
    config.source_lang = "ja"
    config.state_dir = state_dir
    return config


def _run(input_path, state_dir):
    cfg = _config(state_dir)
    orch = Application(cfg, client=FakeClient(handler=routing_handler))
    store = orch.run(input_path)
    _stamp_formal_prereqs(store)
    return store, cfg


def _stamp_formal_prereqs(store):
    """Direct writer tests stamp title, QA, Repair, and report prerequisites."""
    from trans_novel.pipeline.state import NODE_DETERMINISTIC_QA, NODE_REPAIR, NodeState

    state = store.load_state()
    for node_id in ("titles", NODE_DETERMINISTIC_QA, NODE_REPAIR, "report"):
        state.nodes.setdefault(node_id, NodeState(node_id=node_id, status="succeeded"))
    store.save_state(state)
    return store


class TestAssembleEpub(unittest.TestCase):
    def test_schema4_epub_export_preserves_inline_image(self):
        with tempfile.TemporaryDirectory() as d:
            epub = os.path.join(d, "inline.epub")
            write_inline_sample_epub(epub)
            store, _ = _run(epub, os.path.join(d, "state"))

            persisted = store.load_chapter(0)
            self.assertTrue(persisted.segments)
            self.assertTrue(all(segment.epub_state is not None for segment in persisted.segments))

            output = assemble(store, epub, out_format="epub")
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
            self.assertIsNone(rendered.find(attrs={"data-tn-inline-id": True}))

    def test_schema4_source_epub_rebuild(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub")
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as z:
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertIn("润", html)  # 译文已替换
            self.assertNotIn("data-tn-id", html)  # 占位标记已清除
            self.assertNotIn("綾小路は教室", html)  # 原文已被替换

    def test_source_epub_with_invalid_mimetype_is_normalized(self):
        with tempfile.TemporaryDirectory() as d:
            epub = os.path.join(d, "invalid-mimetype.epub")
            write_sample_epub(epub)
            with zipfile.ZipFile(epub) as source:
                members = [(info.filename, source.read(info)) for info in source.infolist()]
            members = members[1:] + members[:1]
            with zipfile.ZipFile(epub, "w", zipfile.ZIP_DEFLATED) as source:
                for name, data in members:
                    source.writestr(
                        name,
                        b"application/epub+zip\r\n" if name == "mimetype" else data,
                    )

            store, _ = _run(epub, os.path.join(d, "state"))
            output = assemble(store, epub, out_format="epub")

            with zipfile.ZipFile(output) as translated:
                mimetype = translated.infolist()[0]
                self.assertEqual(mimetype.filename, "mimetype")
                self.assertEqual(mimetype.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(translated.read("mimetype"), b"application/epub+zip")
            self.assertTrue(store.load_epub_verification()["passed"])

    def test_vertical_epub_preserves_original_layout(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "vertical.epub")
            _write_vertical_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                opf = z.read("OEBPS/content.opf").decode("utf-8")
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
                css = z.read("OEBPS/style.css").decode("utf-8")
            self.assertIn("<dc:language>zh-Hans</dc:language>", opf)
            self.assertIn('page-progression-direction="rtl"', opf)
            self.assertIn("writing-mode: vertical-rl", css)
            self.assertIn('class="vrtl"', html)
            self.assertNotIn("writing-mode: horizontal-tb", html)

    def test_phase9_table_bilingual_round_trip_validates_both_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.epub")
            mono_path = os.path.join(directory, "mono.epub")
            bilingual_path = os.path.join(directory, "bilingual.epub")
            source_first_path = os.path.join(directory, "bilingual-source-first.epub")
            write_phase9_epub(source)

            store, _ = _run(source, os.path.join(directory, "state"))
            manifest = store.load_manifest()
            chapters = [store.load_chapter(entry["index"]) for entry in manifest["chapters"]]
            self.assertEqual(manifest["meta"]["epub_schema"], 4)
            self.assertFalse(
                os.path.exists(os.path.join(directory, "state", "resource_templates.json"))
            )
            all_segments = [segment for chapter in chapters for segment in chapter.segments]
            self.assertTrue(all(segment.epub_state is not None for segment in all_segments))
            self.assertTrue(
                all(
                    segment.epub_state.resource_href == segment.resource_href
                    for segment in all_segments
                )
            )
            mono = assemble(store, source, mono_path, out_format="epub")
            bilingual = assemble(
                store,
                source,
                bilingual_path,
                out_format="epub",
                bilingual=True,
                order="target_first",
            )
            source_first = assemble(
                store,
                source,
                source_first_path,
                out_format="epub",
                bilingual=True,
                order="source_first",
            )

            result = validate_epub_triplet(source, mono, bilingual)
            source_first_result = validate_epub_triplet(source, mono, source_first)
            self.assertTrue(result["structural_pass"], result)
            self.assertTrue(result["mono"]["structural_pass"], result)
            self.assertTrue(result["bilingual"]["structural_pass"], result)
            self.assertTrue(source_first_result["structural_pass"], source_first_result)
            self.assertTrue(source_first_result["mono"]["structural_pass"], source_first_result)
            self.assertTrue(
                source_first_result["bilingual"]["structural_pass"],
                source_first_result,
            )

            def read_chapter(path: str) -> BeautifulSoup:
                with zipfile.ZipFile(path) as archive:
                    return BeautifulSoup(
                        archive.read("OEBPS/text/chapter-1.xhtml"),
                        "html.parser",
                    )

            def read_links(path: str, member: str) -> list[str]:
                with zipfile.ZipFile(path) as archive:
                    soup = BeautifulSoup(archive.read(member), "html.parser")
                return sorted(str(anchor["href"]) for anchor in soup.find_all("a", href=True))

            expected_chapter_one_links = [
                "chapter-2.xhtml#chapter-two",
                "chapter-2.xhtml#footnote-1",
            ]
            expected_chapter_two_links = ["chapter-1.xhtml#ref-1"]
            for output in (mono, bilingual, source_first):
                self.assertEqual(
                    read_links(output, "OEBPS/text/chapter-1.xhtml"),
                    expected_chapter_one_links,
                )
                self.assertEqual(
                    read_links(output, "OEBPS/text/chapter-2.xhtml"),
                    expected_chapter_two_links,
                )

            target_soup = read_chapter(bilingual)
            source_first_soup = read_chapter(source_first)
            for soup, source_before_target in (
                (target_soup, False),
                (source_first_soup, True),
            ):
                table = soup.find("table")
                self.assertIsNotNone(table)
                assert isinstance(table, Tag)
                for cell in table.find_all(["th", "td"]):
                    source_node = cell.find(class_="tn-source", recursive=False)
                    self.assertIsNotNone(source_node)
                    assert isinstance(source_node, Tag)
                    source_index = cell.contents.index(source_node)
                    target_index = next(
                        index
                        for index, child in enumerate(cell.contents)
                        if not isinstance(child, Tag) and str(child).strip()
                    )
                    if source_before_target:
                        self.assertLess(source_index, target_index)
                    else:
                        self.assertGreater(source_index, target_index)

                list_items = soup.select("ul > li")
                self.assertTrue(list_items)
                self.assertTrue(
                    all(item.find(class_="tn-source", recursive=False) for item in list_items)
                )
                blockquote = soup.find("blockquote")
                self.assertIsNotNone(blockquote)
                assert isinstance(blockquote, Tag)
                self.assertIsNotNone(blockquote.find(class_="tn-source", recursive=False))

    def test_phase9_direct_br_round_trip_validates_both_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            original = os.path.join(directory, "original.epub")
            source = os.path.join(directory, "direct-br.epub")
            write_phase9_epub(original)
            with (
                zipfile.ZipFile(original) as source_zip,
                zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as output_zip,
            ):
                for info in source_zip.infolist():
                    data = source_zip.read(info.filename)
                    if info.filename == "OEBPS/text/chapter-2.xhtml":
                        data = data.replace(
                            b'<p id="body-two">Second chapter.</p>',
                            b'<p id="body-two">Line one<br/>Line two</p>',
                        )
                    compression = (
                        zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
                    )
                    output_zip.writestr(info.filename, data, compression)

            store, _ = _run(source, os.path.join(directory, "state"))
            mono = assemble(
                store,
                source,
                os.path.join(directory, "mono.epub"),
                out_format="epub",
            )
            target_first = assemble(
                store,
                source,
                os.path.join(directory, "bilingual-target-first.epub"),
                out_format="epub",
                bilingual=True,
                order="target_first",
            )
            source_first = assemble(
                store,
                source,
                os.path.join(directory, "bilingual-source-first.epub"),
                out_format="epub",
                bilingual=True,
                order="source_first",
            )
            for bilingual in (target_first, source_first):
                result = validate_epub_triplet(source, mono, bilingual)
                self.assertTrue(result["structural_pass"], result)
                self.assertTrue(result["mono"]["structural_pass"], result)
                self.assertTrue(result["bilingual"]["structural_pass"], result)


class TestHeadingNumberInWriter(unittest.TestCase):
    """章节标题编号数字风格（阿拉伯 → 汉字）在槽位分配前统一。"""

    def test_epub_heading_and_toc_normalized(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            ch = store.load_chapter(0)
            complete = normalize_heading_numbering("第5章 迫击炮")
            ch.segments[0].assign_translation(
                distribute_slot_translation(ch.segments[0].epub_state, complete),
            )
            store.save_chapter(ch)
            m = store.load_manifest()
            m["chapters"][0]["title_translated"] = "第5章 迫击炮"  # 目录/nav 用的标题译名
            store.save_manifest(m)

            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertIn("<h1>第五章 迫击炮</h1>", html)
            self.assertNotIn("第5章", html)

    def test_toc_entries_title_translated_normalized_in_nav(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            with zipfile.ZipFile(ep, "r") as source_zip:
                entries = {
                    info.filename: source_zip.read(info.filename) for info in source_zip.infolist()
                }
                entries["OEBPS/nav.xhtml"] = (
                    b'<html xmlns:epub="http://www.idpf.org/2007/ops">'
                    b'<body><nav epub:type="toc"><ol>'
                    b'<li><a href="ch2.xhtml">old</a></li>'
                    b"</ol></nav></body></html>"
                )
            with zipfile.ZipFile(ep, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, data in entries.items():
                    zf.writestr(
                        name,
                        data,
                        zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED,
                    )
            store, _ = _run(ep, os.path.join(d, "state"))
            m = store.load_manifest()
            meta = m.setdefault("meta", {})
            meta["toc_entries"] = [
                {
                    "toc_path": "OEBPS/nav.xhtml",
                    "node_index": 0,
                    "kind": "nav",
                    "raw_href": "ch2.xhtml",
                    "href": "ch2.xhtml",
                    "title_translated": "第8章 尾声",
                }
            ]
            store.save_manifest(m)

            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
            self.assertIn("第八章 尾声", nav)
            self.assertNotIn("第8章", nav)


class TestAssembleEpubPhysicalResourceGrouping(unittest.TestCase):
    def test_two_logical_chapters_sharing_one_physical_file_both_translated(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "nested.epub")
            write_nested_toc_epub(ep, toc_kind="ncx")
            store, _ = _run(ep, os.path.join(d, "state"))
            m = store.load_manifest()
            self.assertEqual(len(m["chapters"]), 2)
            chapters = [store.load_chapter(c["index"]) for c in m["chapters"]]
            # 两个逻辑章共享同一物理资源
            self.assertEqual({ch.href for ch in chapters}, {"OEBPS/body.xhtml"})

            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                self.assertEqual(names.count("OEBPS/body.xhtml"), 1)  # 该物理文件只写一次
                html = z.read("OEBPS/body.xhtml").decode("utf-8")
            for ch in chapters:
                for seg in ch.segments:
                    self.assertIn(seg.target, html)
            self.assertNotIn("Part I intro.", html)
            self.assertNotIn("Part II intro.", html)
            self.assertNotIn("data-tn-id", html)

    def test_chapter_spanning_two_files_backfills_both(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "cross.epub")
            write_cross_resource_toc_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            m = store.load_manifest()
            self.assertEqual(len(m["chapters"]), 2)
            first_chapter = store.load_chapter(m["chapters"][0]["index"])
            resources_touched = {seg.resource_href for seg in first_chapter.segments}
            # 第一个逻辑章跨 one.xhtml 与 two.xhtml 两个物理文件
            self.assertEqual(resources_touched, {"OEBPS/one.xhtml", "OEBPS/two.xhtml"})

            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                one_html = z.read("OEBPS/one.xhtml").decode("utf-8")
                two_html = z.read("OEBPS/two.xhtml").decode("utf-8")
            for seg in first_chapter.segments:
                target_html = one_html if seg.resource_href == "OEBPS/one.xhtml" else two_html
                self.assertIn(seg.target, target_html)
            self.assertNotIn("One.", one_html)
            self.assertNotIn("Two.", two_html)


class TestAssembleEpubLegacySchema(unittest.TestCase):
    """旧 EPUB 状态必须在导出边界拒绝；请重新开始 schema 4 翻译。"""

    def test_schema1_chapter_template_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "legacy.epub")
            write_sample_epub(ep)
            chapter = Chapter(index=0, title="Legacy", href="OEBPS/ch1.xhtml")
            run_dir = os.path.join(d, "state")
            os.makedirs(os.path.join(run_dir, "chapters"), exist_ok=True)
            with open(os.path.join(run_dir, "chapters", "ch0.json"), "w", encoding="utf-8") as f:
                json.dump(chapter.to_dict(), f, ensure_ascii=False)
            manifest = {
                "title": "Legacy",
                "fmt": "epub",
                "source_path": ep,
                "source_lang": "ja",
                "target_lang": "zh",
                "meta": {},
                "chapters": [{"index": 0, "title": chapter.title, "href": chapter.href}],
            }
            with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False)

            store = RunStore(run_dir)
            with self.assertRaisesRegex(ValueError, "Unsupported EPUB state schema"):
                store.load_state()
            with self.assertRaisesRegex(ValueError, "fresh translation"):
                assemble(store, ep, out_format="epub")


class TestTocRoutingAndSchemaFindings(unittest.TestCase):
    """目录回填须按 toc_entries 路由，旧 EPUB schema 须立即拒绝。"""

    def test_schema2_state_is_rejected_before_resource_access(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            chapter = Chapter(index=0, title="第一章", href="OEBPS/ch1.xhtml")
            run_dir = os.path.join(d, "state")
            os.makedirs(os.path.join(run_dir, "chapters"), exist_ok=True)
            with open(os.path.join(run_dir, "chapters", "ch0.json"), "w", encoding="utf-8") as f:
                json.dump(chapter.to_dict(), f, ensure_ascii=False)
            manifest = {
                "title": "Schema2Legacy",
                "fmt": "epub",
                "source_path": ep,
                "source_lang": "ja",
                "target_lang": "zh",
                "meta": {"epub_schema": 2, "toc_entries": []},
                "chapters": [{"index": 0, "title": chapter.title, "href": chapter.href}],
            }
            with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False)
            with open(os.path.join(run_dir, "resource_templates.json"), "w", encoding="utf-8") as f:
                json.dump({"OEBPS/ch1.xhtml": "<html/>"}, f)

            store = RunStore(run_dir)
            with self.assertRaisesRegex(ValueError, "Unsupported EPUB state schema"):
                store.load_state()
            with self.assertRaisesRegex(ValueError, "fresh translation"):
                assemble(store, ep, out_format="epub")

    def test_ncx_named_with_non_ncx_extension_still_backfills(self):
        """NCX 文件名不是 .ncx（如 toc.xml）时，须按 toc_entries 中的 toc_path + kind 路由并回填；
        否则，仅按后缀判断会漏掉该文件（解析端已能根据根节点将其识别为 NCX 目录）。"""
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "toc-xml.epub")
            write_nested_toc_epub(ep, ncx_filename="toc.xml")
            store, _ = _run(ep, os.path.join(d, "state"))
            m = store.load_manifest()
            meta = m.setdefault("meta", {})
            for entry in meta["toc_entries"]:
                entry["title_translated"] = f"译-{entry['title']}"
            store.save_manifest(m)

            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                toc_xml = z.read("OEBPS/toc.xml").decode("utf-8")
            self.assertIn("译-PART I", toc_xml)
            self.assertIn("译-PART II", toc_xml)
            self.assertNotIn(">PART I<", toc_xml)

    def test_nav_without_epub_type_attribute_still_backfills(self):
        """NAV 缺少 epub:type="toc"（解析端 nav_toc_scopes 可兼容此情况）时，只要 toc_entries 中有
        toc_path 与该文件精确匹配的目录项，就必须执行精确回填，不能因 _is_nav 未识别出 NAV 而跳过。"""
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "navtypeless.epub")
            write_epub_type_less_nav_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            m = store.load_manifest()
            meta = m.setdefault("meta", {})
            for entry in meta["toc_entries"]:
                entry["title_translated"] = "译-One"
            store.save_manifest(m)

            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                nav_html = z.read("OEBPS/nav.xhtml").decode("utf-8")
            self.assertIn("译-One", nav_html)
            self.assertNotIn(">One<", nav_html)


if __name__ == "__main__":
    unittest.main()
