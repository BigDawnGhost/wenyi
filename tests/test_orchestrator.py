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


def _fix_json(user: str, replacement: str) -> str:
    """从 Fixer 请求回显身份字段，并构造完整临时替换协议。"""

    def field(name: str) -> str:
        match = re.search(rf"^{name}:\s*(.+)$", user, re.MULTILINE)
        if match is None:
            raise AssertionError(f"Fixer prompt missing {name}")
        return match.group(1).strip()

    return json.dumps(
        {
            "segment_ref": field("segment_ref"),
            "before_hash": field("before_hash"),
            "issue_ids": json.loads(field("issue_ids")),
            "replacement": replacement,
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
            with open(
                os.path.join(result["debug_dir"], "usage.json"),
                encoding="utf-8",
            ) as file:
                debug_usage = json.load(file)
            self.assertGreater(debug_usage["totals"]["calls"], 0)
            self.assertIn("Reviewer", debug_usage["by_stage"])
            self.assertNotIn("Translator", debug_usage["by_stage"])
            self.assertGreater(
                client.usage_summary()["by_stage"]["Reviewer"]["calls"],
                0,
            )

    def test_debug_review_saves_initial_and_unresolved_suggestions(self):
        with tempfile.TemporaryDirectory() as d:
            result = self._run(d)
            with open(
                os.path.join(result["debug_dir"], "initial_issues.json"),
                encoding="utf-8",
            ) as file:
                initial = json.load(file)
            with open(
                os.path.join(result["debug_dir"], "unresolved_issues.json"),
                encoding="utf-8",
            ) as file:
                unresolved = json.load(file)
            self.assertTrue(initial)
            self.assertTrue(unresolved)
            self.assertFalse(os.path.exists(os.path.join(result["debug_dir"], "final_issues.json")))

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

    def test_review_progress_advances_per_chunk_and_resets_for_blind_round(self):
        """Review 按块推进段落数；下一轮盲审和 clean 确认使用独立阶段。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_chars_per_batch = 8  # 每章拆成多个顶层审校块
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 2
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)
            events: list[tuple[int, int, str]] = []

            orch.run_review(
                txt,
                progress=lambda done, total, label: events.append((done, total, label)),
            )

        first = [(done, total) for done, total, label in events if label == "全书审校 R1"]
        second = [(done, total) for done, total, label in events if label == "全书盲审 R2"]
        clean = [(done, total) for done, total, label in events if label == "干净确认"]
        self.assertGreater(len(first), 2)
        self.assertGreater(len(second), 2)
        for stage in (first, second):
            self.assertEqual(stage[0][0], 0)
            self.assertEqual(stage[-1][0], stage[-1][1])
            self.assertEqual([done for done, _ in stage], sorted(done for done, _ in stage))
            self.assertTrue(any(0 < done < total for done, total in stage))
        self.assertEqual(clean, [(1, 2), (2, 2)])

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
            with open(
                os.path.join(debug_root, runs[-1], "usage.json"),
                encoding="utf-8",
            ) as file:
                debug_usage = json.load(file)
            self.assertEqual(debug_usage["totals"]["calls"], 1)
            self.assertEqual(debug_usage["totals"]["total_tokens"], 8)
            self.assertEqual(debug_usage["by_stage"]["Reviewer"]["calls"], 1)
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
                self.assertIsNotNone(usage)
                assert usage is not None
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
            cfg.pipeline.review_fix_loop = False
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)
            progress_events: list[tuple[int, int, str]] = []

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
                result = orch.run_review(
                    txt,
                    progress=lambda done, total, label: progress_events.append(
                        (done, total, label)
                    ),
                )

            with open(
                os.path.join(result["debug_dir"], "pre_arbitration_issues.json"),
                encoding="utf-8",
            ) as file:
                before = json.load(file)
            with open(
                os.path.join(result["debug_dir"], "unresolved_issues.json"),
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
            self.assertEqual(
                [(done, total) for done, total, label in progress_events if label == "冲突仲裁 R1"],
                [(0, 1), (1, 1)],
            )

    def test_shadow_fix_is_blindly_rereviewed_with_translation_context(self):
        """临时修订进入下一轮 Reviewer，且不修改任何正式状态文件。"""
        review_users: list[str] = []
        fix_users: list[str] = []

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                review_users.append(user)
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "语义不完整",
                            "suggestion": "补全原文信息",
                        },
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "人物译名不统一",
                            "suggestion": "沿用术语表译名",
                        },
                    ]
                    if len(review_users) == 1
                    else []
                )
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                fix_users.append(user)
                return _fix_json(user, "影子修订译文。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            client = MeteredFakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)
            store = orch.run(txt)
            formal_before = {
                str(path.relative_to(store.run_dir)): path.read_bytes()
                for path in Path(store.run_dir).rglob("*")
                if path.is_file() and "debug" not in path.relative_to(store.run_dir).parts
            }

            progress_events: list[tuple[int, int, str]] = []
            result = orch.run_review(
                txt,
                progress=lambda done, total, label: progress_events.append((done, total, label)),
            )

            formal_after = {
                str(path.relative_to(store.run_dir)): path.read_bytes()
                for path in Path(store.run_dir).rglob("*")
                if path.is_file() and "debug" not in path.relative_to(store.run_dir).parts
            }
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )
            patches = json.loads(
                Path(result["debug_dir"], "patches.json").read_text(encoding="utf-8")
            )
            not_rereported = json.loads(
                Path(result["debug_dir"], "not_rereported_patches.json").read_text(encoding="utf-8")
            )
            fixer_trace_exists = Path(
                result["debug_dir"],
                "rounds/001/fixers/ch0-text1.json",
            ).is_file()

        self.assertEqual(formal_after, formal_before)
        self.assertEqual(len(fix_users), 1)
        self.assertIn("语义不完整", fix_users[0])
        self.assertIn("人物译名不统一", fix_users[0])
        self.assertIn("风格指南：克制", fix_users[0])
        self.assertIn("全书概览", fix_users[0])
        self.assertIn("本章梗概", fix_users[0])
        self.assertTrue(
            any("影子修订译文。" in user for user in review_users[2:]),
            "第二轮基础 Reviewer 必须直接读取影子译文",
        )
        self.assertEqual(result["review_issues"], [])
        self.assertEqual(summary["termination"], "clean_confirmed")
        self.assertEqual(summary["review_round_count"], 3)
        self.assertEqual(summary["fix_round_count"], 1)
        self.assertEqual(summary["not_rereported_patch_count"], 1)
        self.assertEqual(len(patches), 1)
        self.assertEqual(len(patches[0]["issue_ids"]), 2)
        self.assertEqual(patches[0]["status"], "not_rereported")
        self.assertEqual(patches[0]["not_rereported_in_round"], 2)
        self.assertEqual(not_rereported, patches)
        self.assertFalse(Path(result["debug_dir"], "verified_patches.json").exists())
        self.assertTrue(fixer_trace_exists)
        self.assertEqual(
            [(done, total) for done, total, label in progress_events if label == "影子修订 R1"],
            [(0, 1), (1, 1)],
        )
        self.assertIn("全书盲审 R2", [label for _, _, label in progress_events])

    def test_clean_first_pass_requires_an_independent_confirmation(self):
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            if "译文审校" in system:
                review_calls += 1
                return _review_json(messages[-1]["content"], [])
            if "谨慎修订编辑" in system:
                fix_calls += 1
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(review_calls, 4)  # 两章 × 两轮全书盲审
        self.assertEqual(fix_calls, 0)
        self.assertEqual(summary["review_round_count"], 2)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(summary["termination"], "clean_confirmed")

    def test_last_allowed_fix_still_gets_two_clean_review_passes(self):
        """最后一轮 Fix 后仍须保留两次完整盲审的执行容量。"""
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": f"第 {call // 2 + 1} 轮仍需调整",
                            "suggestion": "继续改写",
                        }
                    ]
                    if call in {0, 2}
                    else []
                )
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                fix_calls += 1
                return _fix_json(user, f"影子版本 {fix_calls}。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(review_calls, 8)  # 两章 × 四轮全书 Review
        self.assertEqual(fix_calls, 2)
        self.assertEqual(summary["review_round_count"], 4)
        self.assertEqual(summary["fix_round_count"], 2)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(summary["termination"], "clean_confirmed")

    def test_clean_pass_before_a_fix_does_not_consume_post_fix_confirmation(self):
        """Fix 前的 clean 不能挤掉补丁后的两次独立确认。"""
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "第二轮才发现的问题",
                            "suggestion": "修订该段",
                        }
                    ]
                    if call == 2
                    else []
                )
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                fix_calls += 1
                return _fix_json(user, "迟发现问题的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 1
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(review_calls, 8)  # clean → issue/fix → clean → clean
        self.assertEqual(fix_calls, 1)
        self.assertEqual(summary["review_round_count"], 4)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(summary["termination"], "clean_confirmed")

    def test_failed_fixer_issue_survives_when_other_patch_passes_review(self):
        """部分 Fixer 失败的问题不能因下一轮漏报而被当成 clean。"""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "第一段需修订",
                            "suggestion": "修订第一段",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "第二段需修订",
                            "suggestion": "修订第二段",
                        },
                    ]
                    if call == 0
                    else []
                )
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                if "ch0:text0:" in user:
                    return _fix_json(user, "第一段影子修订。")
                return ""
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )
            patches = json.loads(
                Path(result["debug_dir"], "patches.json").read_text(encoding="utf-8")
            )
            failures = json.loads(
                Path(result["debug_dir"], "fix_failures.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["termination"], "unresolved_fixes")
        self.assertEqual(summary["blocked_issue_count"], 1)
        self.assertEqual(summary["issue_count"], 1)
        self.assertEqual(len(result["review_issues"]), 1)
        self.assertEqual(result["review_issues"][0]["index"], 1)
        self.assertEqual(result["review_issues"][0]["fix_failure"]["reason"], "malformed_json")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["status"], "not_rereported")
        self.assertEqual(len(failures), 1)

    def test_blocked_issue_survives_different_patch_on_same_segment(self):
        """同段的新补丁不能顺带清除未被其覆盖的历史 Fix 失败问题。"""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                call = review_calls
                review_calls += 1
                if call == 0:
                    issues = [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "用于推动循环的第一段问题",
                            "suggestion": "先修订第一段",
                        },
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "旧术语问题",
                            "suggestion": "沿用既有译名",
                        },
                    ]
                elif call == 2:
                    issues = [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "同段后来发现的新误译问题",
                            "suggestion": "补全该段语义",
                        }
                    ]
                else:
                    issues = []
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                if "旧术语问题" in user:
                    return ""
                if "用于推动循环的第一段问题" in user:
                    return _fix_json(user, "第一段影子修订。")
                if "同段后来发现的新误译问题" in user:
                    return _fix_json(user, "第二段针对新误译的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(review_calls, 6)  # 三轮全书 Review，每轮两章
        self.assertEqual(summary["termination"], "unresolved_fixes")
        self.assertEqual(summary["blocked_issue_count"], 1)
        self.assertEqual(len(result["review_issues"]), 1)
        issue = result["review_issues"][0]
        self.assertEqual((issue["chapter"], issue["index"]), (0, 1))
        self.assertEqual(issue["type"], "terminology")
        self.assertEqual(issue["detail"], "旧术语问题")
        self.assertEqual(issue["fix_failure"]["reason"], "malformed_json")

    def test_same_segment_same_type_fix_failures_remain_distinct(self):
        """同段同类型的两个独立问题不能在 blocked 状态中互相覆盖。"""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            if "译文审校" in system:
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "人物动作漏译",
                            "suggestion": "补回人物动作",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "时间关系误译",
                            "suggestion": "修正时间关系",
                        },
                    ]
                    if review_calls == 1
                    else []
                )
                return _review_json(messages[-1]["content"], issues)
            if "谨慎修订编辑" in system:
                return ""
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["termination"], "no_progress")
        self.assertEqual(summary["blocked_issue_count"], 2)
        self.assertEqual(
            {issue["detail"] for issue in result["review_issues"]},
            {"人物动作漏译", "时间关系误译"},
        )
        self.assertTrue(
            all(
                issue["fix_failure"]["reason"] == "malformed_json"
                for issue in result["review_issues"]
            )
        )

    def test_rereported_blocked_issue_is_deduplicated_across_rounds(self):
        """同一逻辑问题重报时只保留最新证据，并继承先前 Fix 失败信息。"""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                call = review_calls
                review_calls += 1
                issues = []
                if call == 0:
                    issues.append(
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "用于进入下一轮的问题",
                            "suggestion": "先修订第一段",
                        }
                    )
                if call in {0, 2}:
                    issues.append(
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "跨轮重复的术语问题",
                            "suggestion": "统一使用既有译名",
                        }
                    )
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                if "跨轮重复的术语问题" in user:
                    return ""
                if "用于进入下一轮的问题" in user:
                    return _fix_json(user, "用于进入下一轮的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 1
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        repeated = [
            issue
            for issue in result["review_issues"]
            if (
                issue.get("chapter"),
                issue.get("index"),
                issue.get("type"),
                issue.get("detail"),
            )
            == (0, 1, "terminology", "跨轮重复的术语问题")
        ]
        self.assertEqual(summary["termination"], "max_rounds")
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["review_round"], 2)
        self.assertEqual(repeated[0]["fix_failure"]["reason"], "malformed_json")
        self.assertEqual(repeated[0]["fix_failure"]["review_round"], 1)

    def test_rejected_cycle_patch_does_not_clear_prior_blocked_issue(self):
        """候选 overlay 被判定为循环时，适用补丁也不能解除历史 blocked。"""
        review_calls = 0
        original_targets: dict[int, str] = {}

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                call = review_calls
                review_calls += 1
                if call == 0:
                    issues = [
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "循环前已阻塞的术语问题",
                            "suggestion": "使用既有译名",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "先生成版本 B",
                            "suggestion": "改写第二段",
                        },
                    ]
                elif call == 2:
                    issues = [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "同段新发现的问题",
                            "suggestion": "保持第一段原译",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "把第二段恢复原译",
                            "suggestion": "恢复第二段",
                        },
                    ]
                else:
                    issues = []
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                if "循环前已阻塞的术语问题" in user:
                    return ""
                if "先生成版本 B" in user:
                    return _fix_json(user, "影子版本 B。")
                if "同段新发现的问题" in user:
                    return _fix_json(user, original_targets[0])
                if "把第二段恢复原译" in user:
                    return _fix_json(user, original_targets[1])
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_chars_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            store = orch.run(txt)
            chapter = store.load_chapter(0)
            original_targets = {
                0: chapter.text_segments[0].target or "",
                1: chapter.text_segments[1].target or "",
            }

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["termination"], "cycle_detected")
        blocked = [
            issue
            for issue in result["review_issues"]
            if issue.get("detail") == "循环前已阻塞的术语问题"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["fix_failure"]["reason"], "malformed_json")

    def test_final_summary_includes_blocked_conflicts_and_fallbacks(self):
        """最终汇总必须从全部 unresolved 重建，不能只读取最后一轮 clean 结果。"""

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "谨慎修订编辑" in system:
                return _fix_json(user, "用于进入盲审轮的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = True
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            def fake_review(
                text_segs,
                terms,
                *,
                chapter_index,
                review_round,
                **kwargs,
            ):
                if review_round != 1:
                    return []
                if chapter_index == 0:
                    return [
                        {
                            "index": 0,
                            "_chunk_id": "conflict-a",
                            "type": "terminology",
                            "detail": "第一种译名",
                            "suggestion": "统一为绫小路",
                            "consistency": {
                                "kind": "term",
                                "subject_source": "綾小路",
                                "proposed_value": "绫小路",
                            },
                        },
                        {
                            "index": 1,
                            "_chunk_id": "fallback-a",
                            "type": "mistranslation",
                            "detail": "Agent 未完成核验",
                            "suggestion": "人工复核",
                            "agent_fallback": True,
                            "fallback_reason": "max_rounds",
                        },
                        {
                            "index": 2,
                            "_chunk_id": "fixable-a",
                            "type": "mistranslation",
                            "detail": "用于进入下一轮的可修问题",
                            "suggestion": "修订该段",
                        },
                    ]
                return [
                    {
                        "index": 0,
                        "_chunk_id": "conflict-b",
                        "type": "terminology",
                        "detail": "第二种译名",
                        "suggestion": "统一为绫小路君",
                        "consistency": {
                            "kind": "term",
                            "subject_source": "綾小路",
                            "proposed_value": "绫小路君",
                        },
                    }
                ]

            with patch.object(orch, "_review_chapter", side_effect=fake_review):
                result = orch.run_review(txt)

            debug_dir = Path(result["debug_dir"])
            summary = json.loads((debug_dir / "summary.json").read_text(encoding="utf-8"))
            result_json = json.loads((debug_dir / "result.json").read_text(encoding="utf-8"))
            conflicts = json.loads((debug_dir / "conflicts.json").read_text(encoding="utf-8"))
            residual = json.loads(
                (debug_dir / "residual_conflicts.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["termination"], "unresolved_fixes")
        self.assertEqual(summary["issue_count"], 3)
        self.assertEqual(summary["blocked_issue_count"], 3)
        self.assertEqual(summary["conflict_count"], 1)
        self.assertEqual(summary["unresolved_conflict_count"], 1)
        self.assertEqual(summary["fallback_agent_count"], 1)
        self.assertEqual(result_json["summary"], summary)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(residual), 1)
        unresolved_ids = {issue["issue_id"] for issue in result["review_issues"]}
        conflict_ids = set(conflicts[0]["issue_ids"])
        residual_ids = set(residual[0]["issue_ids"])
        self.assertEqual(conflict_ids, residual_ids)
        self.assertTrue(conflict_ids <= unresolved_ids)

    def test_shadow_loop_detects_a_b_a_oscillation(self):
        review_calls = 0
        fix_calls = 0
        original_target = ""

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "译文审校" in system:
                current = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "仍需调整",
                            "suggestion": "改写",
                        }
                    ]
                    if current % 2 == 0
                    else []
                )
                return _review_json(user, issues)
            if "谨慎修订编辑" in system:
                replacement = "影子版本 B。" if fix_calls == 0 else original_target
                fix_calls += 1
                return _fix_json(user, replacement)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            store = orch.run(txt)
            original_target = store.load_chapter(0).text_segments[1].target or ""

            result = orch.run_review(txt)
            summary = json.loads(
                Path(result["debug_dir"], "summary.json").read_text(encoding="utf-8")
            )

        self.assertEqual(review_calls, 4)  # 两章 × 两轮，未进入第三轮
        self.assertEqual(fix_calls, 2)
        self.assertEqual(summary["review_round_count"], 2)
        self.assertEqual(summary["termination"], "cycle_detected")


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
