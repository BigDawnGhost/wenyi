"""编排器端到端 + 断点续跑测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator, _normalize_lang
from trans_novel.pipeline.runstore import (
    REVIEW_DONE,
    REVIEW_FAILED,
    REVIEW_PENDING,
    STATUS_DONE,
    STATUS_PENDING,
)
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
    def test_prepare_retries_after_analysis_failure(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            def fail_analysis(messages, tier, json_mode):
                raise RuntimeError("temporary model failure")

            with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
                Orchestrator(cfg, client=FakeClient(handler=fail_analysis)).prepare(txt)

            run_dirs = [
                os.path.join(cfg.state_dir, name)
                for name in os.listdir(cfg.state_dir)
            ]
            self.assertEqual(len(run_dirs), 1)
            self.assertFalse(os.path.isfile(os.path.join(run_dirs[0], "manifest.json")))

            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).prepare(txt)
            self.assertTrue(store.exists())
            self.assertTrue(store.load_manifest()["initialized"])
            self.assertIsNotNone(store.load_analysis())

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
            chapter_indices = [chapter["index"] for chapter in m["chapters"]]
            expected_total, expected_done = orch2._progress_counts(
                store, chapter_indices
            )
            progress_events: list[tuple[int, int, str]] = []
            store2 = orch2.run(
                txt,
                progress=lambda done, total, label: progress_events.append(
                    (done, total, label)
                ),
            )
            m2 = store2.load_manifest()
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m2["chapters"]))
            chapter_label = Orchestrator._chapter_progress_label(
                store.load_chapter(1).title, 1
            )
            first_chapter_progress = next(
                event for event in progress_events if event[2] == chapter_label
            )
            self.assertEqual(
                first_chapter_progress,
                (expected_done, expected_total, chapter_label),
            )


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
            first_target = ch2.text_segments[0].target
            last_target = ch2.text_segments[-1].target
            self.assertIsNotNone(first_target)
            self.assertIsNotNone(last_target)
            assert first_target is not None
            assert last_target is not None
            self.assertTrue(first_target.startswith("R1"))
            self.assertTrue(last_target.startswith("R2"))

    def test_resume_splits_mixed_batch_after_budget_change(self):
        """大批次内只缺一段时，也不能覆盖同批已有译文。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.polish = False

            first_client = FakeClient(handler=self._tr_handler("R1"))
            store = Orchestrator(cfg, client=first_client).run(
                txt, only_chapter=0
            )
            chapter = store.load_chapter(0)
            chapter.text_segments[-1].target = ""
            store.save_chapter(chapter)
            store.set_chapter_status(0, STATUS_PENDING)

            # 改变预算后，新分批仍可能把已完成段与空段放在一起。
            cfg.segment.max_chars_per_batch = 50_000
            second_client = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=second_client).run(txt, only_chapter=0)

            self.assertEqual(_translated_para_count(second_client.calls), 1)
            resumed = store.load_chapter(0).text_segments
            self.assertTrue(all((segment.target or "").startswith("R1") for segment in resumed[:-1]))
            self.assertTrue((resumed[-1].target or "").startswith("R2"))


class TestBookUnderstanding(unittest.TestCase):
    def _translate_user(self, calls) -> str:
        """返回最后一次翻译调用送进模型的 user 文本。"""
        for c in reversed(calls):
            if "文学翻译" in c["messages"][0]["content"]:
                return c["messages"][-1]["content"]
        return ""

    def test_prepass_builds_and_injects(self):
        """预扫产出逐章梗概+全书概览，并注入翻译 prompt。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            # 逐章梗概落盘到 chapter.meta
            self.assertTrue(store.load_chapter(0).meta.get("source_digest"))
            # 全书概览落盘到 analysis
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))

            # 翻译 prompt 注入了全书概览 / 本章梗概块（且非「（无）」占位）
            user = self._translate_user(client.calls)
            self.assertIn("【全书概览】", user)
            self.assertIn("【本章梗概】", user)
            self.assertIn("全书概览", user)   # fake 概览正文
            self.assertIn("本章梗概", user)   # fake 逐章梗概正文

    def test_prepare_for_translation_builds_understanding_without_targets(self):
        """准备模式落盘分析、初始术语和全书概览，但不翻译正文。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            store = Orchestrator(
                cfg,
                client=client,
            ).prepare_for_translation(txt)

            manifest = store.load_manifest()
            self.assertTrue(store.load_analysis())
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))
            glossary = GlossaryStore(store.glossary_path)
            try:
                self.assertGreater(glossary.stats()["terms"], 0)
            finally:
                glossary.close()
            for item in manifest["chapters"]:
                chapter = store.load_chapter(item["index"])
                self.assertTrue(chapter.meta.get("source_digest"))
                self.assertTrue(
                    all(segment.target is None for segment in chapter.segments)
                )
            translate_calls = [
                call
                for call in client.calls
                if "文学翻译" in call["messages"][0]["content"]
            ]
            self.assertEqual(translate_calls, [])

    def test_prescan_parallel(self):
        """并行预扫：多线程 digest 后各章梗概按章序落盘，翻译注入正常。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.prescan_concurrency = 3

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            m = store.load_manifest()
            for c in m["chapters"]:
                self.assertTrue(store.load_chapter(c["index"]).meta.get("source_digest"))
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))
            user = self._translate_user(client.calls)
            self.assertIn("【本章梗概】", user)

    def test_resume_skips_prepass(self):
        """续跑：梗概/概览已落盘，不再产生预扫调用。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            Orchestrator(cfg, client=FakeClient(handler=routing_handler)).run(txt)

            c2 = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=c2).run(txt)
            prepass = [c for c in c2.calls
                       if "梗概员" in c["messages"][0]["content"]
                       or "概览员" in c["messages"][0]["content"]]
            self.assertEqual(len(prepass), 0)

    def test_toggle_off(self):
        """关闭 book_understanding：不预扫，prompt 用「（无）」占位。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.book_understanding = False

            client = FakeClient(handler=routing_handler)
            store = Orchestrator(cfg, client=client).run(txt)

            self.assertFalse(store.load_chapter(0).meta.get("source_digest"))
            self.assertFalse((store.load_analysis() or {}).get("book_synopsis"))
            prepass = [c for c in client.calls
                       if "梗概员" in c["messages"][0]["content"]
                       or "概览员" in c["messages"][0]["content"]]
            self.assertEqual(len(prepass), 0)


class TestRunSteps(unittest.TestCase):
    def test_subset_only_assemble(self):
        """run_steps 步骤子集：仅回填时不应再产生翻译调用（幂等）。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
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


class TestReviewReporting(unittest.TestCase):
    """独立最终审校 + 严重项自动重译（autofix_severe）。"""

    # 样例首段「第一章　出会い」7 字；fix 需在 3-21 字间（比值 0.3-3.0）方可通过长度校验
    FIX_TEXT = "第一章 邂逅"   # 7 字，比值 1.0

    def _handler(self, fix_text):
        """审校每块报 index 0 漏译；带【审校意见】的翻译调用返回定向重译文。"""
        def handler(messages, tier, json_mode):
            sys = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in sys:
                return json.dumps({"issues": [
                    {"index": 0, "type": "missing", "detail": "漏了一句", "suggestion": "补上"}
                ]}, ensure_ascii=False)
            if "文学翻译" in sys and "【审校意见】" in user:
                return json.dumps({"translations": [fix_text]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)
        return handler

    def _run(self, d, *, autofix, fix_text=None):
        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        cfg.pipeline.autofix_severe = autofix
        handler = self._handler(fix_text or self.FIX_TEXT)
        orch = Orchestrator(cfg, client=FakeClient(handler=handler))
        orch.run(txt)
        return orch.run_review(txt, autofix=autofix)["store"]

    def test_run_does_not_call_reviewer_even_for_only_chapter(self):
        """翻译主流程和 only_chapter 都不再隐式触发最终审校。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            store = Orchestrator(cfg, client=client).run(txt, only_chapter=0)
            Orchestrator(cfg, client=client).run(txt)

            review_calls = [
                call for call in client.calls
                if "译文审校" in call["messages"][0]["content"]
            ]
            self.assertEqual(review_calls, [])
            self.assertTrue(
                all(
                    chapter["review_status"] == REVIEW_PENDING
                    for chapter in store.load_manifest()["chapters"]
                )
            )

    def test_autofix_adopts_retranslation(self):
        """autofix 开：严重项定向重译被采纳 → target 更新、fixed=True。"""
        with tempfile.TemporaryDirectory() as d:
            store = self._run(d, autofix=True)
            ch = store.load_chapter(0)
            flagged = [i for i in ch.meta["review_issues"] if i.get("type") == "missing"]
            self.assertTrue(flagged)
            self.assertTrue(all(i.get("fixed") is True for i in flagged))
            self.assertTrue(all(i.get("stage") == "review" for i in flagged))
            self.assertTrue(all("chapter" in i for i in flagged))
            self.assertEqual(ch.text_segments[0].target, self.FIX_TEXT)
            glossary = GlossaryStore(store.glossary_path)
            try:
                self.assertEqual(
                    glossary.tm_lookup(ch.text_segments[0].source),
                    self.FIX_TEXT,
                )
            finally:
                glossary.close()

    def test_autofix_off_reports_only(self):
        """autofix 关：仅上报 fixed=False，正文不动。"""
        with tempfile.TemporaryDirectory() as d:
            store = self._run(d, autofix=False)
            ch = store.load_chapter(0)
            flagged = [i for i in ch.meta["review_issues"] if i.get("type") == "missing"]
            self.assertTrue(flagged)
            self.assertTrue(all(i.get("fixed") is False for i in flagged))
            self.assertNotEqual(ch.text_segments[0].target, self.FIX_TEXT)

    def test_autofix_rejects_short_retranslation(self):
        """重译结果过短（疑漏译）→ 不采纳，fixed=False，保留原译。"""
        with tempfile.TemporaryDirectory() as d:
            store = self._run(d, autofix=True, fix_text="短")
            ch = store.load_chapter(0)
            flagged = [i for i in ch.meta["review_issues"] if i.get("type") == "missing"]
            self.assertTrue(flagged)
            self.assertTrue(all(i.get("fixed") is False for i in flagged))
            self.assertNotEqual(ch.text_segments[0].target, "短")

    def test_review_index_mapping(self):
        """整章多块审校时，块内 index 正确映射回章内段号。"""
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return json.dumps({"issues": [
                    {"index": 0, "type": "missing", "detail": "x", "suggestion": ""}
                ]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 8   # 审校块预算=24 → 每段自成一块
            cfg.pipeline.autofix_severe = False
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            store = orch.run_review(txt, autofix=False)["store"]
            ch = store.load_chapter(0)
            idxs = sorted(i["index"] for i in ch.meta["review_issues"]
                          if i.get("type") == "missing")
            # 每块报 index 0 → 映射后应为各块首段的章内段号（0,1,2,...互不相同）
            self.assertEqual(idxs, list(range(len(ch.text_segments))))

    def test_review_accepts_numeric_string_index(self):
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return json.dumps(
                    {"issues": [
                        {"index": "0", "type": "missing",
                         "detail": "x", "suggestion": ""}
                    ]},
                    ensure_ascii=False,
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.autofix_severe = False

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            store = orch.run_review(txt, autofix=False)["store"]

            issues = store.load_chapter(0).meta["review_issues"]
            self.assertTrue(issues)
            self.assertEqual(issues[0]["index"], 0)

    def test_review_warns_when_index_is_invalid(self):
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return json.dumps(
                    {"issues": [
                        {"index": "unknown", "type": "missing",
                         "detail": "x", "suggestion": ""}
                    ]}
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.autofix_severe = False

            with self.assertWarnsRegex(RuntimeWarning, "无效审校索引"):
                orch = Orchestrator(cfg, client=FakeClient(handler=handler))
                orch.run(txt)
                store = orch.run_review(txt, autofix=False)["store"]

            self.assertEqual(store.load_chapter(0).meta["review_issues"], [])

    def test_review_digest_skips_unchanged_chapters_and_force_reruns(self):
        """摘要相同则跳过已审章节；force 会忽略摘要并重新审校。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            orch.run_review(txt, autofix=False)
            first_count = sum(
                "译文审校" in call["messages"][0]["content"]
                for call in client.calls
            )
            manifest = orch.prepare(txt).load_manifest()
            self.assertTrue(
                all(chapter["review_status"] == REVIEW_DONE
                    for chapter in manifest["chapters"])
            )
            store = orch.prepare(txt)
            self.assertTrue(
                all(store.load_chapter(chapter["index"]).meta.get("review_digest")
                    for chapter in manifest["chapters"])
            )

            orch.run_review(txt, autofix=False)
            unchanged_count = sum(
                "译文审校" in call["messages"][0]["content"]
                for call in client.calls
            )
            self.assertEqual(unchanged_count, first_count)

            orch.run_review(txt, force=True, autofix=False)
            forced_count = sum(
                "译文审校" in call["messages"][0]["content"]
                for call in client.calls
            )
            self.assertGreater(forced_count, unchanged_count)

    def test_review_rejects_incomplete_book(self):
        """独立最终审校要求全书所有章节均已翻译完成。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            store = orch.run(txt, only_chapter=0)

            with self.assertRaisesRegex(ValueError, "所有章节先完成翻译"):
                orch.run_review(txt)

            self.assertEqual(
                store.load_manifest()["chapters"][0]["review_status"],
                REVIEW_PENDING,
            )

    def test_review_without_state_rejects_pdf_before_conversion(self):
        """PDF 尚无翻译状态时不得调用转换服务或创建空状态目录。"""
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "book.pdf")
            with open(pdf, "wb") as file:
                file.write(b"%PDF-1.4\n")
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)

            with (
                patch("trans_novel.pipeline.orchestrator.load_document") as loader,
                self.assertRaisesRegex(ValueError, "尚无翻译进度"),
            ):
                orch.run_review(pdf)

            loader.assert_not_called()
            self.assertEqual(client.calls, [])
            self.assertFalse(os.path.exists(cfg.state_dir))

    def test_review_without_state_does_not_initialize_text_book(self):
        """普通输入尚无状态时只允许本地定位，不得触发分析或初始化。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            with self.assertRaisesRegex(ValueError, "尚无翻译进度"):
                Orchestrator(cfg, client=client).run_review(txt)

            self.assertEqual(client.calls, [])
            self.assertFalse(os.path.exists(cfg.state_dir))

    def test_reviewer_failure_marks_chapter_failed(self):
        """审校调用失败必须显式标记 failed，不能伪装成零问题。"""
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                raise RuntimeError("review service unavailable")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            store = orch.run(txt)

            with self.assertRaisesRegex(RuntimeError, "review service unavailable"):
                orch.run_review(txt)

            self.assertEqual(
                store.load_manifest()["chapters"][0]["review_status"],
                REVIEW_FAILED,
            )


class TestStyleAnalysis(unittest.TestCase):
    def _long_doc(self, d):
        from trans_novel.ingest.segmenter import load_document
        txt = os.path.join(d, "long.txt")
        chapters = []
        for i in range(3):
            # 段落勿以「第N章」开头，避免被 TXT reader 的章标题启发式误判
            body = "\n\n".join(f"章{i}の段落{j}です。" + "あ" * 60 for j in range(8))
            chapters.append(f"# 第{i}章\n\n{body}")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("\n\n".join(chapters))
        return load_document(txt, "ja", "zh")

    def test_sample_text_multipoint(self):
        """labeled=True 多点采样带三个标注；labeled=False 为纯源文单段。"""
        with tempfile.TemporaryDirectory() as d:
            doc = self._long_doc(d)
            labeled = Orchestrator._sample_text(doc)
            for tag in ("【开头样章】", "【中部样章】", "【结尾样章】"):
                self.assertIn(tag, labeled)
            plain = Orchestrator._sample_text(doc, labeled=False)
            self.assertNotIn("样章】", plain)
            self.assertIn("章0の段落0です", plain)

    def test_sample_text_short_book_dedup(self):
        """单章书：三个采样点重合，只取一次、不重复。"""
        with tempfile.TemporaryDirectory() as d:
            from trans_novel.ingest.segmenter import load_document
            txt = os.path.join(d, "short.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write("# 唯一章\n\n" + "长段落。" + "あ" * 300)
            doc = load_document(txt, "ja", "zh")
            sample = Orchestrator._sample_text(doc)
            self.assertEqual(sample.count("【开头样章】"), 1)
            self.assertNotIn("【中部样章】", sample)
            self.assertNotIn("【结尾样章】", sample)

    def test_style_brief_new_fields(self):
        """style_brief 渲染新风格维度；旧 analysis（缺新字段）不报错不输出。"""
        from trans_novel.agents.analyzer import Analyzer
        from trans_novel.llm.providers.fake import FakeClient as FC

        cfg = _config("state")
        ana = Analyzer(FC(), cfg)
        brief = ana.style_brief({
            "genre": "校园", "pacing": "短句为主", "register": "口语",
            "dialogue_style": "语气词丰富", "narration": "第一人称",
        })
        self.assertIn("句式节奏：短句为主", brief)
        self.assertIn("语域：口语", brief)
        self.assertIn("对话风格：语气词丰富", brief)
        self.assertIn("叙事：第一人称", brief)
        # 旧格式：只有老字段
        old = ana.style_brief({"genre": "校园", "tone": "冷峻"})
        self.assertIn("体裁：校园", old)
        self.assertNotIn("句式节奏", old)


class TestGlossaryScope(unittest.TestCase):
    def _run_with_terms(self, d, scope):
        from trans_novel.glossary.store import GlossaryStore, GlossaryTerm

        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        cfg.pipeline.glossary_scope = scope

        orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
        store = orch.prepare(txt)
        g = GlossaryStore(store.glossary_path)
        # ①正文外人物 ②无关术语（source/alias 均不在正文）③alias 在正文出现
        g.upsert_term(GlossaryTerm(source="外部人物X", target="外部译名",
                                   type="人物"))
        g.upsert_term(GlossaryTerm(source="無関係用語", target="无关术语", type="术语"))
        g.upsert_term(GlossaryTerm(source="ホリキタ", target="堀北译名",
                                   aliases=["堀北"], type="术语"))
        g.close()

        client = FakeClient(handler=routing_handler)
        Orchestrator(cfg, client=client).run(txt)
        return ["\n".join(m["content"] for m in c["messages"])
                for c in client.calls
                if "文学翻译" in c["messages"][0]["content"]]

    def test_chapter_scope_prunes(self):
        """chapter：正文外条目剔除，alias 命中的条目保留。"""
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d, "chapter")
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertNotIn("外部人物X", p)  # 本章未出现：剔除
                self.assertNotIn("無関係用語", p)  # 本章未出现：剔除
                self.assertIn("ホリキタ", p)      # 别名「堀北」在正文：保留

    def test_full_scope_keeps_all(self):
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d, "full")
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertIn("外部人物X", p)
                self.assertIn("無関係用語", p)
                self.assertIn("ホリキタ", p)

    def test_batch_glossary_refreshes_following_prompts(self):
        """批次翻译后实时抽取术语，后续批次 prompt 立即带上新称谓。"""
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "文学翻译" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.M))
                return json.dumps({"translations": ["小夏帆" for _ in range(n)]},
                                  ensure_ascii=False)
            if "术语" in system and "抽取器" in system and "夏帆ちゃん" in user and "小夏帆" in user:
                return json.dumps({"terms": [
                    {"source": "夏帆ちゃん", "target": "小夏帆",
                     "type": "称谓", "aliases": ["夏帆"], "note": "亲昵称呼"}
                ]}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n"
                    "「夏帆ちゃん」と母親が言った。\n\n"
                    "夏帆ちゃんは窓の外を見た。\n"
                )
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.consistency_qa = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 10

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(txt)

            translate_prompts = [
                "\n".join(m["content"] for m in c["messages"])
                for c in client.calls
                if "文学翻译" in c["messages"][0]["content"]
            ]
            self.assertGreaterEqual(len(translate_prompts), 3)
            self.assertIn("夏帆ちゃん → 小夏帆", translate_prompts[-1])

    def test_resume_recovers_batch_glossary_checkpoints_from_events(self):
        """旧状态续跑时复用抽取事件，不为已完成批次重复调用模型。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.consistency_qa = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 8

            store = Orchestrator(
                cfg, client=FakeClient(handler=routing_handler)
            ).run(txt, only_chapter=0)
            checkpoints = store.completed_batch_glossary_keys(0)
            self.assertGreater(len(checkpoints), 1)

            # 章已完成但状态被恢复为 pending：续跑应从事件日志识别已抽取批次。
            store.set_chapter_status(0, STATUS_PENDING)

            labels: list[str] = []
            glossary_labels: list[str] = []

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                if "术语" in system and "抽取器" in system:
                    glossary_labels.append(labels[-1])
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(
                txt,
                only_chapter=0,
                progress=lambda _done, _total, label: labels.append(label),
            )

            glossary_calls = [
                call for call in client.calls
                if "术语" in call["messages"][0]["content"]
                and "抽取器" in call["messages"][0]["content"]
            ]
            # 已译批次全部跳过，只保留章末一次兜底抽取。
            self.assertEqual(len(glossary_calls), 1)
            self.assertTrue(glossary_labels)
            self.assertTrue(all(label != "解析文档…" for label in glossary_labels))

    def test_final_glossary_is_available_to_review_prompt(self):
        """后章才抽出的术语，也能用于从第一章开始的最终审校。"""
        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "文学翻译" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.M))
                return json.dumps({"translations": ["小夏帆" for _ in range(n)]},
                                  ensure_ascii=False)
            if "术语" in system and "抽取器" in system and "後半で" in user:
                return json.dumps({"terms": [
                    {"source": "夏帆ちゃん", "target": "小夏帆",
                     "type": "称谓", "aliases": ["夏帆"], "note": "亲昵称呼"}
                ]}, ensure_ascii=False)
            if "术语" in system and "抽取器" in system:
                return json.dumps({"terms": []}, ensure_ascii=False)
            if "术语一致性校准器" in system:
                self.assertIn("「夏帆ちゃん」と母親が言った。", user)
                self.assertIn('"target": "小夏帆"', user)
                return json.dumps({"terms": [
                    {"source": "夏帆ちゃん", "target": "小夏帆"}
                ]}, ensure_ascii=False)
            if "译文审校" in system:
                self.assertIn("夏帆ちゃん → 小夏帆", user)
                return json.dumps({"issues": []}, ensure_ascii=False)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n「夏帆ちゃん」と母親が言った。\n\n"
                    "# 第二章\n\n後半で夏帆ちゃんが再び現れた。\n"
                )
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.consistency_qa = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_chars_per_batch = 200

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            orch.run_review(txt)


class TestTierRouting(unittest.TestCase):
    def test_task_tiers(self):
        """机械任务走 fast 档、判断类走 cheap、翻译走 strong；梗概带 max_tokens 上限。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.backtranslate_sample = 1.0  # 强制触发回译

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)
            orch.run_review(txt)

            expect = {
                "章节梗概员": "fast", "全书概览员": "fast",
                "术语与称呼抽取器": "fast", "回译译者": "fast",
                "译文审校": "cheap", "保真度": "cheap",
                "文学翻译": "strong",
            }
            seen = set()
            for c in client.calls:
                system = c["messages"][0]["content"]
                for marker, tier in expect.items():
                    if marker in system:
                        self.assertEqual(c["tier"], tier, f"{marker} 应走 {tier} 档")
                        seen.add(marker)
                        if marker == "章节梗概员":
                            self.assertEqual(c["max_tokens"], 600)
                        if marker == "全书概览员":
                            self.assertEqual(c["max_tokens"], 1200)
            self.assertEqual(seen, set(expect), "各类调用都应出现")


class TestLangNormalize(unittest.TestCase):
    def test_normalize_lang(self):
        self.assertEqual(_normalize_lang("Japanese"), "ja")
        self.assertEqual(_normalize_lang("日语"), "ja")
        self.assertEqual(_normalize_lang("RU"), "ru")
        self.assertEqual(_normalize_lang("russian"), "ru")
        self.assertEqual(_normalize_lang("fr"), "fr")
        self.assertEqual(_normalize_lang("unknown"), "")
        self.assertEqual(_normalize_lang(""), "")


class TestProgressLabels(unittest.TestCase):
    def test_progress_label_prefers_real_title(self):
        self.assertEqual(Orchestrator._chapter_progress_label("引言", 0), "引言")
        self.assertEqual(Orchestrator._chapter_progress_label("第一章", 1), "第一章")
        self.assertEqual(Orchestrator._chapter_progress_label("", 1), "章节 2")

    def test_consistency_label_prefers_real_title(self):
        from trans_novel.agents.consistency import ConsistencyChecker

        self.assertEqual(ConsistencyChecker._chapter_label("第一章", 1), "第一章")
        self.assertEqual(ConsistencyChecker._chapter_label("", 1), "章节 2")

    def test_progress_covers_preparation_and_output_stages(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            events: list[tuple[int, int, str]] = []
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))

            orch.run_steps(
                txt,
                {"translate", "qa", "report", "assemble"},
                progress=lambda done, total, label: events.append((done, total, label)),
            )

            labels = [label for _, _, label in events]
            expected = [
                "解析文档…",
                "分析全书风格…",
                "预扫章节梗概",
                "生成全书概览…",
                "翻译章节标题…",
                "翻译完成",
                "一致性 QA…",
                "生成报告…",
                "回填译文…",
            ]
            positions = [labels.index(label) for label in expected]
            self.assertEqual(positions, sorted(positions), labels)
            self.assertIn((0, 0, "生成全书概览…"), events)


if __name__ == "__main__":
    unittest.main()
