"""双语输出（原文淡化 + 译文对照）的测试（离线）。"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from bs4 import BeautifulSoup
from bs4.element import Tag
from typer.testing import CliRunner

from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.assemble.writer import (
    _default_out,
    _render_chapter_html,
    assemble,
)
from trans_novel.ingest.models import KIND_HEADING, KIND_TEXT, Chapter, Segment
from tests.sample_data import write_sample_epub, write_sample_txt
from tests.fake_llm import routing_handler


def _required_tag(value: object) -> Tag:
    """Assert a BeautifulSoup lookup succeeded and narrow its static type."""
    if not isinstance(value, Tag):
        raise AssertionError("expected HTML tag")
    return value


def _chapter_with_template() -> Chapter:
    """构造一个带模板锚点的章节：标题 + 三个正文段（正常/译文缺失/译文等于原文）。"""
    template = (
        "<html><body>"
        '<h1 data-tn-id="h0">原标题</h1>'
        '<p data-tn-id="p1">原文一</p>'
        '<p data-tn-id="p2">原文二</p>'
        '<p data-tn-id="p3">原文三</p>'
        "</body></html>"
    )
    segments = [
        Segment(
            index=0, source="原标题", kind=KIND_HEADING, target="译标题", anchor="h0"
        ),
        Segment(index=1, source="原文一", kind=KIND_TEXT, target="译文一", anchor="p1"),
        Segment(index=2, source="原文二", kind=KIND_TEXT, target=None, anchor="p2"),
        Segment(index=3, source="原文三", kind=KIND_TEXT, target="原文三", anchor="p3"),
    ]
    return Chapter(
        index=0, title="标题", segments=segments, template=template, href="ch1.xhtml"
    )


class TestRenderChapterHtmlBilingual(unittest.TestCase):
    def test_bilingual_target_first_inserts_source_and_skips_dedup_cases(self):
        ch = _chapter_with_template()
        html = _render_chapter_html(ch, bilingual=True, order="target_first")

        self.assertNotIn("data-tn-id", html)  # 占位标记已清

        soup = BeautifulSoup(html, "html.parser")
        h1 = _required_tag(soup.find("h1"))
        self.assertEqual(h1.get_text(), "译标题")
        # 标题不应带 tn-source（紧邻的下一个兄弟是 p1 的译文，不是 tn-source 段）
        nxt = _required_tag(h1.find_next_sibling())
        self.assertEqual(nxt.name, "p")
        self.assertNotIn("tn-source", nxt.get("class") or ())

        ps = soup.find_all("p")
        self.assertEqual(
            [p.get_text() for p in ps], ["译文一", "原文一", "原文二", "原文三"]
        )
        self.assertEqual(ps[0].get("class"), None)
        self.assertEqual(
            ps[1]["class"], ["tn-source", "ibooks-dark-theme-use-custom-text-color"]
        )
        # p2（译文缺失回退原文）、p3（译文等于原文）都不应插入 tn-source 段
        self.assertEqual(ps[2].get("class"), None)
        self.assertEqual(ps[3].get("class"), None)

    def test_order_source_first_places_source_before_target(self):
        ch = _chapter_with_template()
        html = _render_chapter_html(ch, bilingual=True, order="source_first")
        soup = BeautifulSoup(html, "html.parser")
        ps = soup.find_all("p")
        self.assertEqual(
            [p.get_text() for p in ps], ["原文一", "译文一", "原文二", "原文三"]
        )
        self.assertEqual(
            ps[0]["class"], ["tn-source", "ibooks-dark-theme-use-custom-text-color"]
        )
        self.assertEqual(ps[1].get("class"), None)

    def test_mono_render_has_no_source_paragraphs(self):
        ch = _chapter_with_template()
        html = _render_chapter_html(ch)  # 默认单语，不应引入 tn-source
        self.assertNotIn("tn-source", html)
        self.assertNotIn("data-tn-id", html)

    def test_list_source_stays_inside_list_item(self):
        ch = Chapter(
            index=0,
            title="列表",
            href="ch1.xhtml",
            template=(
                '<html><body><ul><li data-tn-id="li0">原项目</li></ul></body></html>'
            ),
            segments=[
                Segment(
                    index=0,
                    source="原项目",
                    target="译项目",
                    kind=KIND_TEXT,
                    anchor="li0",
                )
            ],
        )

        html = _render_chapter_html(ch, bilingual=True, order="target_first")
        soup = BeautifulSoup(html, "html.parser")
        ul = _required_tag(soup.find("ul"))
        self.assertEqual([child.name for child in ul.find_all(recursive=False)], ["li"])
        li = _required_tag(ul.find("li", recursive=False))
        source = _required_tag(li.find(class_="tn-source", recursive=False))
        self.assertEqual(source.name, "div")
        self.assertEqual(source.get_text(), "原项目")
        self.assertTrue(li.get_text().startswith("译项目"))

    def test_source_first_list_and_blockquote_stay_in_their_containers(self):
        ch = Chapter(
            index=0,
            title="结构",
            href="ch1.xhtml",
            template=(
                '<html><body><ol><li data-tn-id="li0">原项目</li></ol>'
                '<blockquote data-tn-id="q0">原引用</blockquote></body></html>'
            ),
            segments=[
                Segment(
                    index=0,
                    source="原项目",
                    target="译项目",
                    kind=KIND_TEXT,
                    anchor="li0",
                ),
                Segment(
                    index=1,
                    source="原引用",
                    target="译引用",
                    kind=KIND_TEXT,
                    anchor="q0",
                ),
            ],
        )

        html = _render_chapter_html(ch, bilingual=True, order="source_first")
        soup = BeautifulSoup(html, "html.parser")
        ol = _required_tag(soup.find("ol"))
        self.assertEqual([child.name for child in ol.find_all(recursive=False)], ["li"])
        li = _required_tag(ol.find("li", recursive=False))
        quote = _required_tag(soup.find("blockquote"))
        li_source = _required_tag(li.find(class_="tn-source", recursive=False))
        quote_source = _required_tag(
            quote.find(class_="tn-source", recursive=False)
        )
        self.assertEqual(li_source.get_text(), "原项目")
        self.assertEqual(
            quote_source.get_text(), "原引用"
        )
        self.assertTrue(li.get_text().startswith("原项目"))
        self.assertTrue(quote.get_text().startswith("原引用"))


def _config(state_dir: str, output: dict | None = None):
    raw = {
        "language": {"source": "ja", "target": "zh"},
        "llm": {
            "provider": "fake",
            "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
        },
        "pipeline": {
            "review": True,
            "polish": False,
            "backtranslate_sample": 0.0,
            "consistency_qa": False,
        },
        "paths": {"state_dir": state_dir},
    }
    if output is not None:
        raw["output"] = output
    return Config.from_dict(raw)


def _run(input_path, state_dir, output=None):
    cfg = _config(state_dir, output)
    orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
    return orch.run(input_path), cfg


class TestBuildEpubFromChaptersBilingual(unittest.TestCase):
    def test_bilingual_epub_has_source_paragraphs_and_style(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="epub", bilingual=True)
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as z:
                xhtml_names = [
                    n
                    for n in z.namelist()
                    if n.endswith(".xhtml") and n.startswith("EPUB/")
                ]
                self.assertTrue(xhtml_names)
                bodies = {n: z.read(n).decode("utf-8") for n in xhtml_names}
            all_html = "\n".join(bodies.values())
            self.assertIn("tn-source", all_html)
            self.assertIn("译0", all_html)  # 译文仍在（fake 翻译器返回 译N）
            some_head_has_style = any(
                "tn-bilingual-style" in html
                and "@media (prefers-color-scheme: dark)" in html
                and ".tn-source" in html
                for html in bodies.values()
            )
            self.assertTrue(some_head_has_style)


class TestAssembleTextBilingual(unittest.TestCase):
    def test_bilingual_txt_contains_target_and_source_target_first(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(
                store, txt, out_format="txt", bilingual=True, order="target_first"
            )
            with open(out, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("译1", content)  # 译文（段落1，段落0是标题）
            self.assertIn("綾小路は教室の窓際に座っていた", content)  # 原文
            tgt_pos = content.index("译1")
            src_pos = content.index("綾小路は教室の窓際に座っていた")
            self.assertLess(tgt_pos, src_pos)  # target_first：译文先于原文

    def test_bilingual_txt_source_first_order(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(
                store, txt, out_format="txt", bilingual=True, order="source_first"
            )
            with open(out, encoding="utf-8") as f:
                content = f.read()
            tgt_pos = content.index("译1")
            src_pos = content.index("綾小路は教室の窓際に座っていた")
            self.assertLess(src_pos, tgt_pos)  # source_first：原文先于译文

    def test_mono_txt_has_no_source_text(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="txt")  # 默认单语
            with open(out, encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("綾小路は教室の窓際に座っていた", content)


class TestDefaultOutBilingual(unittest.TestCase):
    def test_bilingual_suffix(self):
        out = _default_out("/tmp/novel.txt", "epub", "", bilingual=True)
        self.assertEqual(os.path.basename(out), "novel.zh-bi.epub")
        self.assertEqual(os.path.dirname(out), "/tmp/output")

    def test_mono_suffix_unchanged(self):
        out = _default_out("/tmp/novel.txt", "epub", "")
        self.assertEqual(os.path.basename(out), "novel.zh.epub")
        self.assertEqual(os.path.dirname(out), "/tmp/output")


class TestOutputConfigParsing(unittest.TestCase):
    def test_defaults(self):
        cfg = Config.from_dict({})
        self.assertTrue(cfg.output.mono)
        self.assertFalse(cfg.output.bilingual)
        self.assertEqual(cfg.output.bilingual_order, "target_first")

    def test_bilingual_off_keeps_mono_default(self):
        cfg = Config.from_dict({"output": {"bilingual": False}})
        self.assertFalse(cfg.output.bilingual)
        self.assertIs(cfg.output.mono, True)


class TestOrchestratorMultiOutput(unittest.TestCase):
    def test_default_config_produces_only_mono(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            result = orch.run_all(txt, out_format="epub")
            outputs = result["outputs"]
            self.assertEqual(len(outputs), 1)
            self.assertEqual(os.path.basename(outputs[0]), "novel.zh.epub")
            for p in outputs:
                self.assertTrue(os.path.isfile(p))
            self.assertEqual(result["output"], outputs[0])

    def test_bilingual_on_produces_mono_and_bilingual_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"), output={"bilingual": True})
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            result = orch.run_all(txt, out_format="epub")
            outputs = result["outputs"]
            self.assertEqual(len(outputs), 2)
            basenames = sorted(os.path.basename(p) for p in outputs)
            self.assertEqual(basenames, ["novel.zh-bi.epub", "novel.zh.epub"])


class TestAssembleEpubTemplateBilingual(unittest.TestCase):
    def test_epub_template_rebuild_bilingual(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub", bilingual=True)
            self.assertEqual(os.path.basename(out), "novel.zh-bi.epub")
            with zipfile.ZipFile(out) as z:
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertNotIn("data-tn-id", html)  # 占位标记已清除
            self.assertIn("tn-source", html)  # 原文淡化块已插入
            self.assertIn("tn-bilingual-style", html)  # 双语样式已注入
            self.assertIn("綾小路は教室の窓際に座っていた", html)  # 原文仍保留


class TestCliBilingualFlags(unittest.TestCase):
    def test_translate_flags_override_output_config(self):
        cfg = Config.from_dict(
            {
                "llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}},
            }
        )
        captured = {}

        class FakeStore:
            def load_usage(self):
                return None

        class FakeOrchestrator:
            def __init__(self, config):
                captured["mono"] = config.output.mono
                captured["bilingual"] = config.output.bilingual

            def run_all(self, input_path, **kwargs):
                return {
                    "report": {
                        "summary": {"chapters_done": 1, "chapters_total": 1, "terms": 0}
                    },
                    "qa_issues": [],
                    "output": "novel.zh.epub",
                    "outputs": ["novel.zh.epub", "novel.zh-bi.epub"],
                    "store": FakeStore(),
                }

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app, ["translate", "input.txt", "--no-mono", "--bilingual"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        flat = result.output.replace("\n", "")
        self.assertFalse(captured["mono"])
        self.assertTrue(captured["bilingual"])
        self.assertIn("novel.zh.epub", flat)
        self.assertIn("novel.zh-bi.epub", flat)

    def test_tools_assemble_produces_mono_and_bilingual_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state_dir = os.path.join(d, "state")
            _, cfg = _run(txt, state_dir)
            with patch("trans_novel.cli._load_config", return_value=cfg):
                result = CliRunner().invoke(
                    app, ["tools", "assemble", txt, "--mono", "--bilingual"]
                )
            self.assertEqual(result.exit_code, 0, result.output)
            flat = result.output.replace("\n", "")
            self.assertIn("novel.zh.epub", flat)
            self.assertIn("novel.zh-bi.epub", flat)
            output_dir = os.path.join(d, "output")
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "novel.zh.epub")))
            self.assertTrue(os.path.isfile(os.path.join(output_dir, "novel.zh-bi.epub")))


if __name__ == "__main__":
    unittest.main()
