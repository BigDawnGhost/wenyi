"""Web 前端后端 + 事件流 + 步骤可选 测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import yaml
from fastapi.testclient import TestClient

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.web.server import create_app
from tests.sample_data import write_sample_txt
from tests.fake_llm import routing_handler


def _cfg_dict(state_dir):
    return {
        "language": {"source": "auto", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
        "pipeline": {"review": True, "polish": True,
                     "backtranslate_sample": 0.0, "consistency_qa": True},
        "concurrency": 3,
        "paths": {"state_dir": state_dir},
    }


class TestRunSteps(unittest.TestCase):
    def test_subset_only_assemble(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            cfg = Config.from_dict(_cfg_dict(os.path.join(d, "state")))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run_steps(txt, {"translate"})
            # 仅回填，不应再翻译
            client2 = FakeClient(handler=routing_handler)
            res = Orchestrator(cfg, client=client2).run_steps(txt, {"assemble"})
            self.assertTrue(res["output"].endswith(".epub"))
            self.assertTrue(os.path.isfile(res["output"]))
            translate_calls = [c for c in client2.calls
                               if "文学翻译" in c["messages"][0]["content"]]
            self.assertEqual(len(translate_calls), 0)


class TestEventStream(unittest.TestCase):
    def test_batch_events_and_fixed(self):
        def handler(messages, tier, json_mode):
            sys = messages[0]["content"]
            if "译文审校" in sys:
                # 报一个漏译 → 不再自动重译，仅作为待人工项上报（fixed=False）
                return json.dumps({"issues": [
                    {"index": 0, "type": "missing", "detail": "漏了一句", "suggestion": "补上"}
                ]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            cfg = Config.from_dict(_cfg_dict(os.path.join(d, "state")))
            evs = []
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run_all(txt, events=evs.append)

            types = {e["type"] for e in evs}
            self.assertIn("batch", types)
            self.assertIn("step", types)
            self.assertIn("done", types)
            batches = [e for e in evs if e["type"] == "batch"]
            # 批次事件含双语对照
            self.assertTrue(all("pairs" in b for b in batches))
            self.assertTrue(any(b["pairs"] for b in batches))
            # 漏译建议被上报但不自动修订（fixed=False），留待人工介入
            flagged = [it for b in batches for it in b["issues"]
                       if it.get("index") == 0 and it.get("type") == "missing"]
            self.assertTrue(flagged)
            self.assertTrue(all(it.get("fixed") is False for it in flagged))


class TestWebApi(unittest.TestCase):
    def _prepare(self, d):
        txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
        cfgd = _cfg_dict(os.path.join(d, "state"))
        cfgpath = os.path.join(d, "cfg.yaml")
        with open(cfgpath, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfgd, f)
        Orchestrator(Config.from_dict(cfgd),
                     client=FakeClient(handler=routing_handler)).run_all(txt)
        return txt, cfgpath

    def test_read_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            txt, cfgpath = self._prepare(d)
            client = TestClient(create_app(config_path=cfgpath))
            st = client.get("/api/state", params={"input": txt}).json()
            self.assertTrue(st["exists"])
            self.assertEqual(len(st["chapters"]), 2)
            gl = client.get("/api/glossary", params={"input": txt}).json()
            self.assertGreaterEqual(len(gl["terms"]), 1)
            ch = client.get("/api/chapter", params={"input": txt, "index": 0}).json()
            self.assertTrue(ch["segments"])
            rev = client.get("/api/revisions", params={"input": txt}).json()
            self.assertIn("review", rev)

    def test_glossary_edit_and_apply(self):
        with tempfile.TemporaryDirectory() as d:
            txt, cfgpath = self._prepare(d)
            app = create_app(config_path=cfgpath)
            client = TestClient(app)
            # 写入一个带别名的术语，再让正文出现别名
            from trans_novel.pipeline.runstore import RunStore, slugify
            from trans_novel.ingest.segmenter import load_document
            doc = load_document(txt, "auto", "zh")
            store = RunStore(os.path.join(d, "state", slugify(doc.title)))
            g = GlossaryStore(store.glossary_path)
            g.upsert_term(GlossaryTerm(source="X", target="甲", aliases=["乙"], type="人物"))
            g.close()
            ch = store.load_chapter(0)
            ch.segments[1].target = "乙来了。"
            store.save_chapter(ch)

            # 应用到正文：乙 → 甲
            r = client.post("/api/glossary/apply", json={"input": txt, "source": "X"}).json()
            self.assertGreaterEqual(r.get("rewritten", 0), 1)
            self.assertEqual(store.load_chapter(0).segments[1].target, "甲来了。")

            # 编辑译法（锁定）
            client.put("/api/glossary/term",
                       json={"input": txt, "source": "X", "target": "甲改", "lock": True})
            g = GlossaryStore(store.glossary_path)
            self.assertEqual(g.get_term("X").target, "甲改")
            self.assertTrue(g.get_term("X").locked)
            g.close()

            # 删除
            client.request("DELETE", "/api/glossary/term", json={"input": txt, "source": "X"})
            g = GlossaryStore(store.glossary_path)
            self.assertIsNone(g.get_term("X"))
            g.close()

    def test_edit_rewrites_text_and_reapply(self):
        from trans_novel.pipeline.runstore import RunStore, slugify
        from trans_novel.ingest.segmenter import load_document

        with tempfile.TemporaryDirectory() as d:
            txt, cfgpath = self._prepare(d)
            client = TestClient(create_app(config_path=cfgpath))
            doc = load_document(txt, "auto", "zh")
            store = RunStore(os.path.join(d, "state", slugify(doc.title)))

            g = GlossaryStore(store.glossary_path)
            g.upsert_term(GlossaryTerm(source="Q", target="老译", aliases=["旧译"], type="人物"))
            g.close()
            ch = store.load_chapter(0)
            ch.segments[1].target = "老译与旧译同行。"
            store.save_chapter(ch)

            # 编辑译法 老译→新译：应自动改写正文里的 老译
            r = client.put("/api/glossary/term",
                           json={"input": txt, "source": "Q", "target": "新译", "lock": True}).json()
            self.assertGreaterEqual(r.get("rewritten", 0), 1)
            self.assertIn("新译", store.load_chapter(0).segments[1].target)

            # 重新应用术语表：别名 旧译 → 当前译法 新译
            r2 = client.post("/api/glossary/reapply", json={"input": txt}).json()
            seg = store.load_chapter(0).segments[1].target
            self.assertNotIn("旧译", seg)
            self.assertEqual(seg, "新译与新译同行。")

    def test_run_and_ws(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt"); write_sample_txt(txt)
            cfgd = _cfg_dict(os.path.join(d, "state"))
            cfgpath = os.path.join(d, "cfg.yaml")
            with open(cfgpath, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfgd, f)
            # 注入 fake handler 到 server 端：通过 provider=fake + 路由 handler
            # server 用 build_client(provider=fake) → 无 handler，会返回空；
            # 这里只验证运行管线与 WS 事件通路，不校验译文内容。
            # 用 with 让 TestClient 的事件循环在 post + ws 期间持续存在
            with TestClient(create_app(config_path=cfgpath)) as client:
                res = client.post("/api/run", json={"input": txt, "steps": ["translate"], "format": "epub"}).json()
                self.assertIn("run_id", res)
                got = []
                with client.websocket_connect(f"/ws/{res['run_id']}") as ws:
                    while True:
                        ev = ws.receive_json()
                        got.append(ev["type"])
                        if ev["type"] == "end":
                            break
            self.assertIn("end", got)


if __name__ == "__main__":
    unittest.main()
