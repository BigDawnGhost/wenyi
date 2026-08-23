"""编排器 façade 契约测试：用 spy services 固定步骤选择、顺序、锁作用域与透传语义。

这里用真实 Orchestrator + 替换后的私有服务（spy），不涉及任何领域 I/O，
专门验证纯流程控制层的路由与组合行为。
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock

from trans_novel.config import Config
from trans_novel.pipeline.orchestrator import Orchestrator


class _RecordingStore:
    """记录锁获取范围与运行级事件的假状态库。"""

    def __init__(self):
        self.lock_events: list[str] = []
        self.assemble_lock_events: list[str] = []
        self.events: list[tuple[str, dict]] = []

    @contextmanager
    def lock(self):
        self.lock_events.append("lock:enter")
        try:
            yield
        finally:
            self.lock_events.append("lock:exit")

    @contextmanager
    def assemble_lock(self):
        self.assemble_lock_events.append("assemble_lock:enter")
        try:
            yield
        finally:
            self.assemble_lock_events.append("assemble_lock:exit")

    def log_event(self, event, **payload):
        self.events.append((event, payload))

    def load_usage(self):
        return None

    def save_usage(self, data):
        self.events.append(("usage_saved", {"usage": data}))


class TestOrchestratorContract(unittest.TestCase):
    """spy services 下的编排契约。"""

    def _orchestrator(self, review=False):
        cfg = Config.from_dict(
            {
                "llm": {"provider": "fake"},
                "pipeline": {"review": review},
            }
        )
        orch = Orchestrator(cfg)
        orch._preparation = MagicMock()
        orch._translation = MagicMock()
        orch._review = MagicMock()
        orch._report = MagicMock()
        orch._assembly = MagicMock()
        # 收尾流程需要可用的术语库作用域（spy 版）。
        orch._report.glossary_scope.side_effect = lambda store, needed: self._glossary_scope()
        return orch

    @staticmethod
    @contextmanager
    def _glossary_scope():
        yield Mock()

    def _manifest(self):
        return {"chapters": [{"index": 0}, {"index": 1}]}

    def test_run_routes_prepare_then_translation_under_lock(self):
        """run：准备 → 语言恢复 → 概览 → 书级锁内翻译，参数与 progress 原样透传。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._preparation.activate.return_value = self._manifest()
        orch._preparation.ensure_understanding.return_value = "全书概览"
        orch._translation.run.return_value = store
        progress = Mock()

        result = orch.run("novel.txt", only_chapter=1, progress=progress)

        self.assertIs(result, store)
        orch._preparation.prepare.assert_called_once_with("novel.txt", progress=progress)
        orch._preparation.activate.assert_called_once_with(store)
        orch._preparation.ensure_understanding.assert_called_once_with(store, progress=progress)
        orch._translation.run.assert_called_once_with(
            store,
            book_synopsis="全书概览",
            only_chapter=1,
            progress=progress,
        )
        self.assertEqual(store.lock_events, ["lock:enter", "lock:exit"])

    def test_run_rejects_unknown_chapter_before_translation(self):
        """不存在的章节编号在校验处短路，翻译服务不被调用，异常原样传播。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._preparation.activate.return_value = {"chapters": [{"index": 0}]}

        with self.assertRaisesRegex(ValueError, "章节编号 7 不存在"):
            orch.run("novel.txt", only_chapter=7)

        orch._preparation.ensure_understanding.assert_not_called()
        orch._translation.run.assert_not_called()

    def test_run_propagates_translation_exception_and_short_circuits(self):
        """翻译异常后阶段短路：概览已生成，但异常原样抛出。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._preparation.activate.return_value = self._manifest()
        orch._preparation.ensure_understanding.return_value = ""
        boom = RuntimeError("翻译失败")

        orch._translation.run.side_effect = boom
        with self.assertRaises(RuntimeError) as ctx:
            orch.run("novel.txt")
        self.assertIs(ctx.exception, boom)
        orch._translation.run.assert_called_once()

    def test_run_review_uses_existing_state_fast_path_under_lock(self):
        """仅审校：locate_existing 快路径 + 书级锁内 run_session，不碰报告/导出。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.locate_existing.return_value = store
        orch._review.session_terms.return_value = ["术语"]
        orch._review.run_session.return_value = Mock(
            issues=[{"x": 1}], changes=[], result={"r": 1}, run_dir="reviews/1"
        )
        progress = Mock()

        result = orch.run_review("novel.txt", progress=progress)

        orch._preparation.locate_existing.assert_called_once_with("novel.txt", progress=progress)
        orch._review.session_terms.assert_called_once_with(store)
        orch._review.run_session.assert_called_once_with(store, ["术语"], progress=progress)
        self.assertEqual(
            result,
            {
                "store": store,
                "review_issues": [{"x": 1}],
                "review_changes": [],
                "review_result": {"r": 1},
                "review_dir": "reviews/1",
            },
        )
        self.assertEqual(store.lock_events, ["lock:enter", "lock:exit"])
        orch._report.build_and_save.assert_not_called()
        orch._assembly.assemble_live.assert_not_called()

    def test_run_steps_review_only_routes_to_review_fast_path(self):
        """run_steps({"review"}) 与独立 run_review 走同一快路径。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.locate_existing.return_value = store
        orch._review.session_terms.return_value = []
        orch._review.run_session.return_value = Mock(
            issues=[], changes=[], result={"r": 1}, run_dir="reviews/2"
        )

        result = orch.run_steps("novel.txt", {"review"})

        orch._preparation.prepare.assert_not_called()
        orch._translation.run.assert_not_called()
        orch._review.run_session.assert_called_once()
        self.assertEqual(result["review_result"], {"r": 1})
        self.assertIsNone(result["report"])

    def test_run_assemble_uses_snapshot_fast_path_without_run_lock(self):
        """仅导出：快照快路径，不获取书级锁，格式参数全部透传。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.locate_existing.return_value = store
        orch._assembly.assemble_snapshot.return_value = ["out.epub"]
        progress = Mock()

        result = orch.run_assemble(
            "novel.txt",
            out_format="epub",
            out_path="out.epub",
            pdf_engine="weasyprint",
            progress=progress,
        )

        orch._preparation.locate_existing.assert_called_once_with("novel.txt", progress=progress)
        orch._assembly.assemble_snapshot.assert_called_once_with(
            store,
            input_path="novel.txt",
            progress=progress,
            out_format="epub",
            out_path="out.epub",
            pdf_engine="weasyprint",
        )
        self.assertEqual(store.lock_events, [])
        self.assertEqual(store.assemble_lock_events, [])
        self.assertEqual(result["output"], "out.epub")
        self.assertEqual(result["outputs"], ["out.epub"])
        self.assertIsNone(result["report"])
        self.assertIsNone(result["review_dir"])

    def test_run_steps_assemble_only_uses_snapshot_fast_path(self):
        """run_steps({"assemble"}) 不等待翻译锁，也不调用 prepare。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.locate_existing.return_value = store
        orch._assembly.assemble_snapshot.return_value = ["out.epub"]

        result = orch.run_steps("novel.txt", {"assemble"}, out_format="txt", pdf_engine="fpdf2")

        orch._preparation.prepare.assert_not_called()
        orch._assembly.assemble_snapshot.assert_called_once_with(
            store,
            input_path="novel.txt",
            progress=None,
            out_format="txt",
            out_path=None,
            pdf_engine="fpdf2",
        )
        self.assertEqual(result["output"], "out.epub")

    def test_run_steps_report_only_uses_prepare_not_locate_existing(self):
        """不含 translate 的组合仍走当前 prepare 行为（不擅自改成 require-existing）。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._report.build_and_save.return_value = {"report": True}

        result = orch.run_steps("novel.txt", {"report"})

        orch._preparation.prepare.assert_called_once_with("novel.txt", progress=None)
        orch._preparation.locate_existing.assert_not_called()
        orch._report.build_and_save.assert_called_once()
        self.assertEqual(result["report"], {"report": True})
        self.assertEqual(store.lock_events, ["lock:enter", "lock:exit"])

    def test_full_pipeline_steps_order_and_result_assembly(self):
        """translate+report+assemble：先翻译，再重新进入锁内执行报告与实时导出。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._preparation.activate.return_value = self._manifest()
        orch._preparation.ensure_understanding.return_value = ""
        orch._translation.run.return_value = store
        orch._report.build_and_save.return_value = {"report": True}
        orch._assembly.assemble_live.return_value = ["out.epub"]

        calls: list[str] = []
        orch._report.build_and_save.side_effect = lambda *a, **k: (
            calls.append("report") or {"report": True}
        )
        orch._assembly.assemble_live.side_effect = lambda *a, **k: (
            calls.append("assemble") or ["out.epub"]
        )
        orch._translation.run.side_effect = lambda *a, **k: calls.append("translate") or store

        result = orch.run_steps("novel.txt", {"translate", "report", "assemble"})

        # 翻译完成后再进入锁内收尾；review 未请求时不触发审校。
        self.assertEqual(calls, ["translate", "report", "assemble"])
        orch._review.run_session.assert_not_called()
        self.assertEqual(
            store.lock_events,
            ["lock:enter", "lock:exit", "lock:enter", "lock:exit"],
        )
        self.assertEqual(
            result,
            {
                "store": store,
                "output": "out.epub",
                "outputs": ["out.epub"],
                "report": {"report": True},
                "review_issues": [],
                "review_changes": [],
                "review_result": None,
                "review_dir": None,
            },
        )

    def test_full_pipeline_with_review_includes_review_between_translate_and_report(self):
        """含 translate 的组合先完成翻译，再重新进入锁内执行 Review、Report 和实时 Assemble。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._preparation.activate.return_value = self._manifest()
        orch._preparation.ensure_understanding.return_value = ""
        orch._translation.run.return_value = store
        orch._review.run_session.return_value = Mock(
            issues=[], changes=[], result={"r": 1}, run_dir="reviews/3"
        )
        orch._report.build_and_save.return_value = {"report": True}
        orch._assembly.assemble_live.return_value = ["out.epub"]

        calls: list[str] = []
        orch._review.run_session.side_effect = lambda *a, **k: (
            calls.append("review")
            or Mock(issues=[], changes=[], result={"r": 1}, run_dir="reviews/3")
        )
        orch._report.build_and_save.side_effect = lambda *a, **k: (
            calls.append("report") or {"report": True}
        )
        orch._assembly.assemble_live.side_effect = lambda *a, **k: (
            calls.append("assemble") or ["out.epub"]
        )
        orch._translation.run.side_effect = lambda *a, **k: calls.append("translate") or store

        result = orch.run_steps("novel.txt", {"translate", "review", "report", "assemble"})

        self.assertEqual(calls, ["translate", "review", "report", "assemble"])
        self.assertEqual(result["review_result"], {"r": 1})
        self.assertEqual(result["report"], {"report": True})

    def test_review_failure_short_circuits_report_and_assemble(self):
        """Review 异常后不再执行报告与导出，异常原样传播。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._preparation.activate.return_value = self._manifest()
        orch._preparation.ensure_understanding.return_value = ""
        orch._translation.run.return_value = store
        boom = RuntimeError("review 失败")
        orch._review.run_session.side_effect = boom

        with self.assertRaises(RuntimeError) as ctx:
            orch.run_steps("novel.txt", {"translate", "review", "report", "assemble"})
        self.assertIs(ctx.exception, boom)
        orch._report.build_and_save.assert_not_called()
        orch._assembly.assemble_live.assert_not_called()

    def test_report_failure_short_circuits_assemble(self):
        """报告异常后不再导出，异常原样传播。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        orch._preparation.activate.return_value = self._manifest()
        orch._preparation.ensure_understanding.return_value = ""
        orch._translation.run.return_value = store
        boom = RuntimeError("report 失败")
        orch._report.build_and_save.side_effect = boom

        with self.assertRaises(RuntimeError) as ctx:
            orch.run_steps("novel.txt", {"translate", "report", "assemble"})
        self.assertIs(ctx.exception, boom)
        orch._assembly.assemble_live.assert_not_called()

    def test_run_all_with_review_enabled_requests_review(self):
        """run_all 在配置开启 Review 时把 review 加入步骤集合。"""
        orch = self._orchestrator(review=True)
        with unittest.mock.patch.object(orch, "run_steps", return_value={"sentinel": True}) as spy:
            result = orch.run_all("novel.txt")
        spy.assert_called_once()
        steps = spy.call_args.args[1]
        self.assertEqual(steps, {"translate", "review", "report", "assemble"})
        self.assertEqual(result, {"sentinel": True})

    def test_run_all_without_review_excludes_review(self):
        """run_all 在配置关闭 Review 时不请求审校。"""
        orch = self._orchestrator(review=False)
        with unittest.mock.patch.object(orch, "run_steps", return_value={}) as spy:
            orch.run_all("novel.txt")
        self.assertEqual(spy.call_args.args[1], {"translate", "report", "assemble"})

    def test_prepare_delegates_thinly(self):
        """prepare 是准备服务的薄委托，参数与返回值原样透传。"""
        orch = self._orchestrator()
        store = _RecordingStore()
        orch._preparation.prepare.return_value = store
        progress = Mock()

        result = orch.prepare("novel.txt", progress=progress)

        self.assertIs(result, store)
        orch._preparation.prepare.assert_called_once_with("novel.txt", progress=progress)

    def test_facade_still_exposes_config_and_client(self):
        """公开契约：.config 与 .client 继续可从 Orchestrator 实例访问。"""
        cfg = Config.from_dict({"llm": {"provider": "fake"}})
        orch = Orchestrator(cfg)
        self.assertIs(orch.config, cfg)
        self.assertIsNotNone(orch.client)


if __name__ == "__main__":
    unittest.main()
