"""双语输出（原文淡化 + 译文对照）的测试（离线）。"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from typer.testing import CliRunner

from tests.fixtures.books import write_sample_epub, write_sample_txt
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble import assemble
from trans_novel.assemble.writer import _default_out
from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application


def _config(state_dir: str, output: dict | None = None):
    raw = {"llm": fake_llm_dict()}
    if output is not None:
        raw["output"] = output
    config = Config.from_dict(raw)
    config.source_lang = "ja"
    config.state_dir = state_dir
    return config


def _run(input_path, state_dir, output=None):
    cfg = _config(state_dir, output)
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


class TestBuildEpubFromChaptersBilingual(unittest.TestCase):
    def test_bilingual_epub_preserves_source_and_target(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="epub", bilingual=True)
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as z:
                xhtml_names = [
                    n for n in z.namelist() if n.endswith(".xhtml") and n.startswith("EPUB/")
                ]
                self.assertTrue(xhtml_names)
                bodies = {n: z.read(n).decode("utf-8") for n in xhtml_names}
            all_html = "\n".join(bodies.values())
            self.assertIn("tn-source", all_html)
            self.assertIn(store.load_chapter(0).segments[0].target, all_html)


class TestAssembleTextBilingual(unittest.TestCase):
    def test_bilingual_txt_contains_target_and_source_target_first(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="txt", bilingual=True, order="target_first")
            with open(out, encoding="utf-8") as f:
                content = f.read()
            target = store.load_chapter(0).segments[1].target
            self.assertIn(target, content)
            self.assertIn("綾小路は教室の窓際に座っていた", content)  # 原文
            tgt_pos = content.index(target)
            src_pos = content.index("綾小路は教室の窓際に座っていた")
            self.assertLess(tgt_pos, src_pos)  # target_first：译文先于原文

    def test_bilingual_txt_source_first_order(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="txt", bilingual=True, order="source_first")
            with open(out, encoding="utf-8") as f:
                content = f.read()
            target = store.load_chapter(0).segments[1].target
            tgt_pos = content.index(target)
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

    def test_mono_suffix_unchanged(self):
        out = _default_out("/tmp/novel.txt", "epub", "")
        self.assertEqual(os.path.basename(out), "novel.zh.epub")


class TestMultiOutput(unittest.TestCase):
    def test_default_config_produces_mono_and_bilingual(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))  # 不传 output -> 默认 mono+bilingual 都开
            orch = Application(cfg, client=FakeClient(handler=routing_handler))
            result = orch.run_all(txt, out_format="epub")
            outputs = result["outputs"]
            self.assertEqual(len(outputs), 2)
            basenames = sorted(os.path.basename(p) for p in outputs)
            self.assertEqual(basenames, ["novel.zh-bi.epub", "novel.zh.epub"])
            for p in outputs:
                self.assertTrue(os.path.isfile(p))
            self.assertEqual(result["output"], outputs[0])

    def test_bilingual_off_produces_single_mono_output(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(
                os.path.join(d, "state"),
                output={"bilingual": {"enabled": False}},
            )
            orch = Application(cfg, client=FakeClient(handler=routing_handler))
            result = orch.run_all(txt, out_format="epub")
            outputs = result["outputs"]
            self.assertEqual(len(outputs), 1)
            self.assertEqual(os.path.basename(outputs[0]), "novel.zh.epub")


class TestAssembleEpubSchema4Bilingual(unittest.TestCase):
    def test_schema4_epub_rebuild_bilingual(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub", bilingual=True)
            self.assertEqual(os.path.basename(out), "novel.zh-bi.epub")
            with zipfile.ZipFile(out) as z:
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertNotIn("data-tn-id", html)  # 占位标记已清除
            self.assertIn("tn-source", html)  # 原文块已插入
            self.assertIn("綾小路は教室の窓際に座っていた", html)  # 原文仍保留


class TestCliBilingualFlags(unittest.TestCase):
    def test_translate_flags_override_output_config(self):
        cfg = Config.from_dict(
            {
                "llm": fake_llm_dict(),
            }
        )
        captured = {}

        class FakeStore:
            def load_usage(self):
                return None

            def load_epub_verification(self):
                return None

        class FakeOrchestrator:
            def __init__(self, config):
                captured["mono"] = config.output.mono
                captured["bilingual"] = config.output.bilingual.enabled

            def run_all(self, input_path, **kwargs):
                return {
                    "report": {"summary": {"chapters_done": 1, "chapters_total": 1, "terms": 0}},
                    "qa_issues": [],
                    "output": "novel.zh.epub",
                    "outputs": ["novel.zh.epub", "novel.zh-bi.epub"],
                    "store": FakeStore(),
                }

        with (
            patch("trans_novel.cli.common.load_config", return_value=cfg),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--no-mono", "--bilingual"])

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
            with patch("trans_novel.cli.common.load_config", return_value=cfg):
                result = CliRunner().invoke(
                    app, ["tools", "assemble", txt, "--mono", "--bilingual"]
                )
            self.assertEqual(result.exit_code, 0, result.output)
            flat = result.output.replace("\n", "")
            self.assertIn("novel.zh.epub", flat)
            self.assertIn("novel.zh-bi.epub", flat)
            self.assertTrue(os.path.isfile(os.path.join(d, "novel.zh.epub")))
            self.assertTrue(os.path.isfile(os.path.join(d, "novel.zh-bi.epub")))


if __name__ == "__main__":
    unittest.main()
