"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from bs4 import BeautifulSoup
from bs4.element import Tag

from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.assemble.writer import _render_chapter_html, assemble
from trans_novel.assemble.report import build_report
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.segmenter import load_document
from trans_novel.ingest.epub_reader import _extract_chapter
from trans_novel.ingest.models import Chapter
from tests.sample_data import (
    write_inline_sample_epub,
    write_sample_epub,
    write_sample_txt,
)
from tests.fake_llm import routing_handler


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
    def test_epub_export_restores_inline_image_from_persisted_meta(self):
        with tempfile.TemporaryDirectory() as d:
            epub = os.path.join(d, "inline.epub")
            write_inline_sample_epub(epub)
            store, _ = _run(epub, os.path.join(d, "state"))

            persisted = store.load_chapter(0)
            inline_segments = [s for s in persisted.segments if "epub_inline" in s.meta]
            self.assertEqual(len(inline_segments), 1)

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
        self.assertIsNone(rendered.find(attrs={"data-tn-inline-id": True}))

    def test_epub_render_restores_inline_images_and_breaks(self):
        html = """<html><body>
<p class="Textbody"><img src="before.jpg"/>Avant<br/>Après<img src="after.jpg"/></p>
<p class="illustration"><img src="standalone.jpg"/></p>
</body></html>"""
        title, segments, template = _extract_chapter(
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
                child.name if getattr(child, "name", None) else str(child)
                for child in paragraph.children
            ],
            ["img", "甲乙", "br", "丙丁", "img"],
        )
        self.assertIsNone(rendered.find(attrs={"data-tn-inline-id": True}))
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
        title, segments, template = _extract_chapter(
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
