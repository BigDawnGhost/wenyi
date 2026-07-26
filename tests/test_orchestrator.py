"""编排器端到端 + 断点续跑测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt
from trans_novel.agents.reviewer import ReviewOutputError
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.usage import UsageSample
from trans_novel.pipeline.orchestrator import Orchestrator, _normalize_lang
from trans_novel.pipeline.runstore import STATUS_DONE, STATUS_PENDING


def _translated_para_count(calls) -> int:
    """统计送进翻译模型的源段总数（按编号行计）。"""
    n = 0
    for c in calls:
        if "文学翻译" in c["messages"][0]["content"]:
            n += len(re.findall(r"^\[(\d+)\]", c["messages"][-1]["content"], re.MULTILINE))
    return n


def _review_json(user: str, issues: list[dict]) -> str:
    """构造带完整性回执的 Reviewer 测试响应。"""
    return json.dumps(
        {
            "issues": issues,
            "reviewed_segments": len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE)),
            "complete": True,
        },
        ensure_ascii=False,
    )


def _config(state_dir: str):
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {"strong": {"model": "p"}, "cheap": {"model": "f"}},
            },
            "segment": {"max_chars_per_batch": 1800},
            "pipeline": {
                "review": True,
                "polish": True,
                "backtranslate_sample": 0.0,
                "consistency_qa": True,
            },
            "paths": {"state_dir": state_dir},
        }
    )


class MeteredFakeClient(FakeClient):
    """每次离线调用都记录一小笔用量，用于验证 Review 用量隔离。"""

    def complete(
        self,
        messages,
        *,
        tier="strong",
        json_mode=False,
        max_tokens=None,
        stage=None,
    ):
        self.usage.record(
            tier,
            UsageSample(
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
                cache_miss_tokens=5,
            ),
            stage,
        )
        return super().complete(
            messages,
            tier=tier,
            json_mode=json_mode,
            max_tokens=max_tokens,
            stage=stage,
        )


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

            run_dirs = [os.path.join(cfg.state_dir, name) for name in os.listdir(cfg.state_dir)]
            self.assertEqual(len(run_dirs), 1)
            self.assertFalse(os.path.isfile(os.path.join(run_dirs[0], "manifest.json")))

            store = Orchestrator(cfg, client=FakeClient(handler=routing_handler)).prepare(txt)
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
            g.close()

            # ── 续跑：所有章已 done，不应再产生翻译调用 ──
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            orch2.run(txt)  # resume 语义
            translate_calls = [
                c for c in client2.calls if "文学翻译" in c["messages"][0]["content"]
            ]
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
            expected_total, expected_done = orch2._progress_counts(store, chapter_indices)
            progress_events: list[tuple[int, int, str]] = []
            store2 = orch2.run(
                txt,
                progress=lambda done, total, label: progress_events.append((done, total, label)),
            )
            m2 = store2.load_manifest()
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m2["chapters"]))
            chapter_label = Orchestrator._chapter_progress_label(store.load_chapter(1).title, 1)
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
                n = len(re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.MULTILINE))
                return json.dumps(
                    {"translations": [f"{tag}译{i}" for i in range(n)]},
                    ensure_ascii=False,
                )
            return routing_handler(messages, tier, json_mode)

        return handler

    def test_resume_skips_done_segments_keeps_their_text(self):
        """中断后续跑：已译完的段原样保留、不重翻；只补译未完成的段。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 8  # 每段≈独立批，便于精确续跑
            cfg.pipeline.polish = False  # 保留翻译标记，便于断言（与续跑无关）

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
            self.assertEqual(_translated_para_count(c2.calls), 1)  # 仅 1 段被重翻

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
            store = Orchestrator(cfg, client=first_client).run(txt, only_chapter=0)
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
            self.assertTrue(
                all((segment.target or "").startswith("R1") for segment in resumed[:-1])
            )
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
            self.assertIn("全书概览", user)  # fake 概览正文
            self.assertIn("本章梗概", user)  # fake 逐章梗概正文

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
                self.assertTrue(all(segment.target is None for segment in chapter.segments))
            translate_calls = [
                call for call in client.calls if "文学翻译" in call["messages"][0]["content"]
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
            prepass = [
                c
                for c in c2.calls
                if "梗概员" in c["messages"][0]["content"]
                or "概览员" in c["messages"][0]["content"]
            ]
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
            prepass = [
                c
                for c in client.calls
                if "梗概员" in c["messages"][0]["content"]
                or "概览员" in c["messages"][0]["content"]
            ]
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
            translate_calls = [
                c for c in client2.calls if "文学翻译" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(translate_calls), 0)


class TestReviewReporting(unittest.TestCase):
    """实验性全书 Agent Review：全量运行且结果只写 Debug 目录。"""

    def _handler(self):
        """审校每块报 index 0 漏译，其它流水线调用沿用通用 Fake 响应。"""

        def handler(messages, tier, json_mode):
            sys = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in sys:
                return _review_json(
                    user,
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "漏了一句",
                            "suggestion": "补上",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        return handler

    def _run(self, d):
        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        orch = Orchestrator(cfg, client=FakeClient(handler=self._handler()))
        orch.run(txt)
        return orch.run_review(txt)

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
                call for call in client.calls if "译文审校" in call["messages"][0]["content"]
            ]
            self.assertEqual(review_calls, [])
            self.assertTrue(
                all("review_status" not in chapter for chapter in store.load_manifest()["chapters"])
            )

    def test_debug_review_never_modifies_body_or_formal_review_state(self):
        """实验 Review 只生成 Debug 建议，不修改任何正式状态文件。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = MeteredFakeClient(handler=self._handler())
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)
            watched = [
                store.manifest_path,
                store.chapter_path(0),
                store.glossary_path,
                store.usage_path,
                store.event_log_path,
                store.report_path,
            ]
            before = {
                path: Path(path).read_bytes() if os.path.exists(path) else None for path in watched
            }
            formal_before = {
                str(path.relative_to(store.run_dir)): path.read_bytes()
                for path in Path(store.run_dir).rglob("*")
                if path.is_file() and "debug" not in path.relative_to(store.run_dir).parts
            }

            result = orch.run_review(txt)
            store = result["store"]
            chapter = store.load_chapter(0)
            self.assertTrue(result["review_issues"])
            self.assertEqual(chapter.meta.get("review_issues", []), [])
            self.assertEqual(
                before,
                {
                    path: Path(path).read_bytes() if os.path.exists(path) else None
                    for path in watched
                },
            )
            formal_after = {
                str(path.relative_to(store.run_dir)): path.read_bytes()
                for path in Path(store.run_dir).rglob("*")
                if path.is_file() and "debug" not in path.relative_to(store.run_dir).parts
            }
            self.assertEqual(formal_after, formal_before)
            self.assertTrue(os.path.isfile(os.path.join(result["debug_dir"], "result.json")))
            self.assertGreater(
                client.usage_summary()["by_stage"]["Reviewer"]["calls"],
                0,
            )

    def test_debug_review_saves_initial_and_final_suggestions(self):
        with tempfile.TemporaryDirectory() as d:
            result = self._run(d)
            with open(
                os.path.join(result["debug_dir"], "initial_issues.json"),
                encoding="utf-8",
            ) as file:
                initial = json.load(file)
            with open(
                os.path.join(result["debug_dir"], "final_issues.json"),
                encoding="utf-8",
            ) as file:
                final = json.load(file)
            self.assertTrue(initial)
            self.assertTrue(final)

    def test_review_only_run_steps_is_also_debug_only(self):
        """内部 review-only 步骤与独立命令一致，不写通用流水线事件。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=MeteredFakeClient(handler=self._handler()))
            store = orch.run(txt)
            formal_before = {
                str(path.relative_to(store.run_dir)): path.read_bytes()
                for path in Path(store.run_dir).rglob("*")
                if path.is_file() and "debug" not in path.relative_to(store.run_dir).parts
            }

            result = orch.run_steps(txt, {"review"})

            formal_after = {
                str(path.relative_to(store.run_dir)): path.read_bytes()
                for path in Path(store.run_dir).rglob("*")
                if path.is_file() and "debug" not in path.relative_to(store.run_dir).parts
            }
            self.assertEqual(formal_after, formal_before)
            self.assertIsNone(result["output"])
            self.assertEqual(result["outputs"], [])
            self.assertTrue(result["review_issues"])
            self.assertTrue(os.path.isfile(os.path.join(result["review_debug_dir"], "run.json")))

    def test_debug_review_does_not_create_formal_report(self):
        with tempfile.TemporaryDirectory() as d:
            result = self._run(d)
            self.assertFalse(os.path.exists(result["store"].report_path))

    def test_review_index_mapping(self):
        """整章多块审校时，块内 index 正确映射回章内段号。"""

        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 8  # 审校块预算=24 → 每段自成一块
            cfg.pipeline.review_agent_loop = False
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            result = orch.run_review(txt)
            idxs = sorted(
                i["index"]
                for i in result["review_issues"]
                if i.get("chapter") == 0 and i.get("type") == "missing"
            )
            segment_count = len(result["store"].load_chapter(0).text_segments)
            # 每块报 index 0 → 映射后应为各块首段的章内段号（0,1,2,...互不相同）
            self.assertEqual(idxs, list(range(segment_count)))

    def test_review_accepts_numeric_string_index(self):
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": "0",
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_agent_loop = False

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            result = orch.run_review(txt)

            issues = result["review_issues"]
            self.assertTrue(issues)
            self.assertEqual(issues[0]["index"], 0)

    def test_review_rejects_invalid_index_instead_of_returning_zero(self):
        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": "unknown",
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_output_retries = 0
            cfg.segment.max_chars_per_batch = 100_000

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            with self.assertRaisesRegex(ReviewOutputError, "invalid_issue_index"):
                orch.run_review(txt)

    def test_review_always_reruns_and_creates_a_new_debug_directory(self):
        """实验模式忽略旧摘要，每次执行都是一轮独立全书 Review。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            first = orch.run_review(txt)
            first_count = sum("译文审校" in call["messages"][0]["content"] for call in client.calls)
            manifest = orch.prepare(txt).load_manifest()
            self.assertTrue(all("review_status" not in chapter for chapter in manifest["chapters"]))

            second = orch.run_review(txt)
            second_count = sum(
                "译文审校" in call["messages"][0]["content"] for call in client.calls
            )
            self.assertEqual(second_count, first_count * 2)
            self.assertNotEqual(first["debug_dir"], second["debug_dir"])

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

            self.assertFalse(os.path.exists(os.path.join(store.run_dir, "debug")))

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

    def test_reviewer_failure_keeps_formal_status_and_writes_failed_debug_run(self):
        """服务故障不污染正式状态，但 Debug run.json 必须留下失败收据。"""

        def handler(messages, tier, json_mode):
            if "译文审校" in messages[0]["content"]:
                raise RuntimeError("review service unavailable")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            client = MeteredFakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)
            usage_before = Path(store.usage_path).read_bytes()
            events_before = Path(store.event_log_path).read_bytes()

            with self.assertRaisesRegex(RuntimeError, "review service unavailable"):
                orch.run_review(txt)

            review_calls = [
                call for call in client.calls if "译文审校" in call["messages"][0]["content"]
            ]
            # 只恢复模型输出协议错误；服务故障不得因拆分逻辑被成倍重试。
            self.assertEqual(len(review_calls), 1)
            self.assertTrue(
                all("review_status" not in chapter for chapter in store.load_manifest()["chapters"])
            )
            debug_root = os.path.join(store.run_dir, "debug")
            runs = sorted(os.listdir(debug_root))
            with open(
                os.path.join(debug_root, runs[-1], "run.json"),
                encoding="utf-8",
            ) as file:
                receipt = json.load(file)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["error_type"], "RuntimeError")
            self.assertEqual(Path(store.usage_path).read_bytes(), usage_before)
            self.assertEqual(Path(store.event_log_path).read_bytes(), events_before)
            self.assertEqual(client.usage_summary()["by_stage"]["Reviewer"]["calls"], 1)

    def test_run_steps_excludes_review_usage_on_success_and_failure(self):
        """组合流水线只持久化 Review 之前的用量，Review 成败都不泄漏。"""
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as d:
                txt = os.path.join(d, "novel.txt")
                write_sample_txt(txt)
                cfg = _config(os.path.join(d, "state"))
                cfg.pipeline.review_agent_loop = False
                base_store = Orchestrator(
                    cfg,
                    client=FakeClient(handler=routing_handler),
                ).run(txt)

                def handler(messages, tier, json_mode):
                    if "译文审校" in messages[0]["content"]:
                        if fail:
                            raise RuntimeError("review failed")
                        return _review_json(messages[-1]["content"], [])
                    return routing_handler(messages, tier, json_mode)

                client = MeteredFakeClient(handler=handler)
                orch = Orchestrator(cfg, client=client)
                client.usage.record(
                    "cheap",
                    UsageSample(
                        prompt_tokens=11,
                        completion_tokens=7,
                        total_tokens=18,
                        cache_miss_tokens=11,
                    ),
                    "PreReview",
                )

                if fail:
                    with self.assertRaisesRegex(RuntimeError, "review failed"):
                        orch.run_steps(txt, {"review", "report"})
                else:
                    result = orch.run_steps(txt, {"review", "report"})
                    self.assertIsNotNone(result["report"])

                usage = base_store.load_usage()
                self.assertEqual(usage["by_stage"]["PreReview"]["calls"], 1)
                self.assertNotIn("Reviewer", usage["by_stage"])
                usage_events = [
                    json.loads(line)
                    for line in Path(base_store.event_log_path)
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if json.loads(line).get("event") == "usage_summary"
                ]
                self.assertTrue(usage_events)
                self.assertNotIn("Reviewer", json.dumps(usage_events, ensure_ascii=False))

    def test_non_review_run_does_not_reuse_previous_debug_directory(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)

            reviewed = orch.run_review(txt)
            reported = orch.run_steps(txt, {"report"})

            self.assertIsNotNone(reviewed["debug_dir"])
            self.assertIsNone(reported["review_debug_dir"])

    def test_conflict_arbitration_changes_final_debug_suggestions(self):
        """终局仲裁会改写落选建议，同时在 Debug 中保留完整审计链。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)

            def fake_review(text_segs, terms, *, chapter_index, **kwargs):
                proposed = "绫小路" if chapter_index == 0 else "绫小路君"
                return [
                    {
                        "index": 0,
                        "_chunk_id": f"ch{chapter_index}-chunk",
                        "type": "terminology",
                        "detail": "译名不统一",
                        "suggestion": f"统一为{proposed}",
                        "consistency": {
                            "kind": "term",
                            "subject_source": "綾小路",
                            "proposed_value": proposed,
                        },
                    }
                ]

            def fake_arbitrate(arbiter, conflict):
                issue_ids = [issue["issue_id"] for issue in conflict["issues"]]
                return {
                    "conflict_id": conflict["conflict_id"],
                    "consistency_key": conflict["consistency_key"],
                    "issue_ids": issue_ids,
                    "status": "suggested",
                    "recommended_value": "绫小路",
                    "reason": "沿用首次译名。",
                    "supported_issue_ids": [issue_ids[0]],
                    "rejected_issue_ids": [issue_ids[1]],
                    "evidence_refs": [],
                }

            with (
                patch.object(orch, "_review_chapter", side_effect=fake_review),
                patch(
                    "trans_novel.pipeline.orchestrator.ReviewConflictArbiter.arbitrate",
                    new=fake_arbitrate,
                ),
            ):
                result = orch.run_review(txt)

            with open(
                os.path.join(result["debug_dir"], "pre_arbitration_issues.json"),
                encoding="utf-8",
            ) as file:
                before = json.load(file)
            with open(
                os.path.join(result["debug_dir"], "final_issues.json"),
                encoding="utf-8",
            ) as file:
                final = json.load(file)
            with open(
                os.path.join(result["debug_dir"], "arbitration_superseded_issues.json"),
                encoding="utf-8",
            ) as file:
                superseded = json.load(file)

            self.assertEqual(len(before), 2)
            self.assertEqual(len(final), 2)
            self.assertEqual(len(superseded), 1)
            self.assertEqual(
                {issue["consistency"]["proposed_value"] for issue in final},
                {"绫小路"},
            )
            self.assertEqual(result["review_issues"], final)


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
        brief = ana.style_brief(
            {
                "genre": "校园",
                "pacing": "短句为主",
                "register": "口语",
                "dialogue_style": "语气词丰富",
                "narration": "第一人称",
            }
        )
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
        g.upsert_term(GlossaryTerm(source="外部人物X", target="外部译名", type="人物"))
        g.upsert_term(GlossaryTerm(source="無関係用語", target="无关术语", type="术语"))
        g.upsert_term(
            GlossaryTerm(source="ホリキタ", target="堀北译名", aliases=["堀北"], type="术语")
        )
        g.close()

        client = FakeClient(handler=routing_handler)
        Orchestrator(cfg, client=client).run(txt)
        return [
            "\n".join(m["content"] for m in c["messages"])
            for c in client.calls
            if "文学翻译" in c["messages"][0]["content"]
        ]

    def test_chapter_scope_prunes(self):
        """chapter：正文外条目剔除，alias 命中的条目保留。"""
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d, "chapter")
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertNotIn("外部人物X", p)  # 本章未出现：剔除
                self.assertNotIn("無関係用語", p)  # 本章未出现：剔除
                self.assertIn("ホリキタ", p)  # 别名「堀北」在正文：保留

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
                n = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
                return json.dumps(
                    {"translations": ["小夏帆" for _ in range(n)]}, ensure_ascii=False
                )
            if (
                "术语" in system
                and "抽取器" in system
                and "夏帆ちゃん" in user
                and "小夏帆" in user
            ):
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "夏帆ちゃん",
                                "target": "小夏帆",
                                "type": "称谓",
                                "aliases": ["夏帆"],
                                "note": "亲昵称呼",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n「夏帆ちゃん」と母親が言った。\n\n夏帆ちゃんは窓の外を見た。\n"
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

            store = Orchestrator(cfg, client=FakeClient(handler=routing_handler)).run(
                txt, only_chapter=0
            )
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
                call
                for call in client.calls
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
                n = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
                return json.dumps(
                    {"translations": ["小夏帆" for _ in range(n)]}, ensure_ascii=False
                )
            if "术语" in system and "抽取器" in system and "後半で" in user:
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "夏帆ちゃん",
                                "target": "小夏帆",
                                "type": "称谓",
                                "aliases": ["夏帆"],
                                "note": "亲昵称呼",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if "术语" in system and "抽取器" in system:
                return json.dumps({"terms": []}, ensure_ascii=False)
            if "术语一致性校准器" in system:
                self.assertIn("「夏帆ちゃん」と母親が言った。", user)
                self.assertIn('"target": "小夏帆"', user)
                return json.dumps(
                    {"terms": [{"source": "夏帆ちゃん", "target": "小夏帆"}]},
                    ensure_ascii=False,
                )
            if "译文审校" in system:
                self.assertIn("夏帆ちゃん → 小夏帆", user)
                return _review_json(user, [])
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
                "章节梗概员": "fast",
                "全书概览员": "fast",
                "术语与称呼抽取器": "fast",
                "回译译者": "fast",
                "译文审校": "cheap",
                "保真度": "cheap",
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
