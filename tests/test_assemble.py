"""回填（TXT / EPUB）、报告、一致性 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.assemble.writer import assemble
from trans_novel.assemble.report import build_report
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest.segmenter import load_document
from tests.sample_data import write_sample_txt, write_sample_epub
from tests.fake_llm import routing_handler


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
            # 文件名用译名（fake 书名译文为「标题0」）
            self.assertTrue(out.endswith(".txt"))
            self.assertIn("标题0", os.path.basename(out))
            with open(out, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("润0", content)  # 译文已写入

    def test_txt_input_to_epub(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            store, _ = _run(txt, os.path.join(d, "state"))
            out = assemble(store, txt, out_format="epub")
            self.assertTrue(out.endswith(".epub"))
            self.assertIn("标题0", os.path.basename(out))
            self.assertTrue(zipfile.is_zipfile(out))
            # 重新解析生成的 EPUB，应能读出章节且含译文
            doc = load_document(out, "ja", "zh")
            self.assertGreaterEqual(len(doc.chapters), 2)
            alltext = "".join(s.source for c in doc.chapters for s in c.text_segments)
            self.assertIn("润", alltext)


class TestAssembleEpub(unittest.TestCase):
    def test_epub_template_rebuild(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            out = assemble(store, ep, out_format="epub")
            self.assertTrue(zipfile.is_zipfile(out))
            with zipfile.ZipFile(out) as z:
                html = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertIn("润0", html)            # 译文已替换
            self.assertNotIn("data-tn-id", html)  # 占位标记已清除
            self.assertNotIn("綾小路は教室", html)  # 原文已被替换


class TestTitleTranslation(unittest.TestCase):
    def test_manifest_and_opf_title_translated(self):
        with tempfile.TemporaryDirectory() as d:
            ep = os.path.join(d, "novel.epub")
            write_sample_epub(ep)
            store, _ = _run(ep, os.path.join(d, "state"))
            # 书名 + 各章标题都译出并写回 manifest（fake：标题0/1/2）
            m = store.load_manifest()
            self.assertEqual(m["title_translated"], "标题0")
            self.assertTrue(all(c.get("title_translated") for c in m["chapters"]))
            out = assemble(store, ep, out_format="epub")
            with zipfile.ZipFile(out) as z:
                opf = z.read("OEBPS/content.opf").decode("utf-8")
            self.assertIn("标题0", opf)            # OPF 书名已改为译名
            self.assertNotIn("サンプル小説", opf)   # 原书名已替换
            self.assertIn("标题0", os.path.basename(out))  # 文件名用译名

    def test_rewrite_targets_propagates_to_titles(self):
        from trans_novel.agents.glossary_auditor import GlossaryAuditor
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            store, cfg = _run(txt, os.path.join(d, "state"))
            # 手动写入含变体的标题译名
            m = store.load_manifest()
            m["title_translated"] = "佳穂传"
            m["chapters"][0]["title_translated"] = "佳穂登场"
            store.save_manifest(m)
            g = GlossaryStore(store.glossary_path)
            GlossaryAuditor._rewrite_targets(store, g, {"佳穂": "佳穗"})
            g.close()
            m2 = store.load_manifest()
            self.assertEqual(m2["title_translated"], "佳穗传")          # 书名已规范
            self.assertEqual(m2["chapters"][0]["title_translated"], "佳穗登场")  # 章名已规范

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
