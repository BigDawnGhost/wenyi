"""编排器端到端 + 断点续跑测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator, _normalize_lang
from trans_novel.pipeline.runstore import RunStore, slugify, STATUS_DONE, STATUS_PENDING
from tests.sample_data import write_sample_txt
from tests.fake_llm import routing_handler


def _translated_para_count(calls) -> int:
    """统计送进翻译模型的源段总数（按编号行计）。"""
    n = 0
    for c in calls:
        if "文学翻译" in c["messages"][0]["content"]:
            n += len(re.findall(r"^\[(\d+)\]", c["messages"][-1]["content"], re.M))
    return n


def _config(state_dir: str):
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
        "segment": {"max_chars_per_batch": 1800},
        "pipeline": {"review": True, "polish": True,
                     "backtranslate_sample": 0.0, "consistency_qa": True},
        "paths": {"state_dir": state_dir},
    })


class TestOrchestrator(unittest.TestCase):
    def test_full_run_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)

            # 全部章节标记 done
            m = store.load_manifest()
            self.assertEqual(len(m["chapters"]), 2)
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m["chapters"]))

            # 每段都有译文（润色后为 "润{i}"）
            ch0 = store.load_chapter(0)
            self.assertTrue(all(s.target for s in ch0.text_segments))

            # 术语抽取写入了「堀北」；分析器种入了「绫小路」
            from trans_novel.glossary.store import GlossaryStore
            g = GlossaryStore(store.glossary_path)
            self.assertIsNotNone(g.get_term("綾小路"))
            self.assertIsNotNone(g.get_term("堀北"))
            self.assertGreater(g.stats()["tm_entries"], 0)  # 翻译记忆库已写入
            g.close()

            # ── 续跑：所有章已 done，不应再产生翻译调用 ──
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            orch2.run(txt)  # resume 语义
            translate_calls = [c for c in client2.calls
                               if "文学翻译" in c["messages"][0]["content"]]
            self.assertEqual(len(translate_calls), 0)

    def test_resume_after_partial(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            # 只翻第 0 章
            store = orch.run(txt, only_chapter=0)
            m = store.load_manifest()
            self.assertEqual(m["chapters"][0]["status"], STATUS_DONE)
            self.assertNotEqual(m["chapters"][1]["status"], STATUS_DONE)

            # 续跑应只补翻第 1 章
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            store2 = orch2.run(txt)
            m2 = store2.load_manifest()
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m2["chapters"]))


class TestSegmentLevelResume(unittest.TestCase):
    def _tr_handler(self, tag):
        """返回带标记的翻译 handler（译文形如 {tag}译{i}），其余走默认路由。"""
        def handler(messages, tier, json_mode):
            if "文学翻译" in messages[0]["content"]:
                n = len(re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.M))
                return json.dumps({"translations": [f"{tag}译{i}" for i in range(n)]},
                                  ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)
        return handler

    def test_resume_skips_done_segments_keeps_their_text(self):
        """中断后续跑：已译完的段原样保留、不重翻；只补译未完成的段。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 8     # 每段≈独立批，便于精确续跑
            cfg.pipeline.polish = False             # 保留翻译标记，便于断言（与续跑无关）

            # 第一次：用 R1 译完第 0 章
            c1 = FakeClient(handler=self._tr_handler("R1"))
            store = Orchestrator(cfg, client=c1).run(txt, only_chapter=0)
            ch = store.load_chapter(0)
            self.assertTrue(all(s.target and s.target.startswith("R1") for s in ch.text_segments))

            # 模拟中断：清空最后一段译文、章状态改回 pending
            ch.segments[-1].target = ""
            store.save_chapter(ch)
            store.set_chapter_status(0, STATUS_PENDING)

            # 第二次：用 R2 续跑——只应补译被清空的那 1 段
            c2 = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=c2).run(txt, only_chapter=0)
            self.assertEqual(_translated_para_count(c2.calls), 1)   # 仅 1 段被重翻

            ch2 = store.load_chapter(0)
            # 之前已译的段仍是 R1（未被跨位置复用、也未重翻），补译段是 R2
            self.assertTrue(ch2.text_segments[0].target.startswith("R1"))
            self.assertTrue(ch2.text_segments[-1].target.startswith("R2"))


class TestLangNormalize(unittest.TestCase):
    def test_normalize_lang(self):
        self.assertEqual(_normalize_lang("Japanese"), "ja")
        self.assertEqual(_normalize_lang("日语"), "ja")
        self.assertEqual(_normalize_lang("RU"), "ru")
        self.assertEqual(_normalize_lang("russian"), "ru")
        self.assertEqual(_normalize_lang("fr"), "fr")
        self.assertEqual(_normalize_lang(""), "")


if __name__ == "__main__":
    unittest.main()
