"""编排器：纯流程控制层，唯一公开的编排 façade。

Orchestrator 只负责：
  * 共享 runtime 与各具体服务（准备/翻译/注释/审校/收尾）的装配；
  * steps 路由、阶段顺序、锁作用域选择、metrics session 包裹、progress 转发；
  * 异常短路与异常传播、统一返回结构。

文档解析、LLM/Agent 调用、状态读写、线程池、术语处理、注释对齐、Review 状态机、
报告、导出、用量和指标等实际操作均位于各领域服务中。编排器不直接依赖
agents / ingest / glossary / assemble / ThreadPoolExecutor，也不直接读写任何
状态文件。依赖方向固定为：

    CLI → Orchestrator → Runtime / Preparation / Translation / Annotation /
                          Review / Finalization → agents / ingest / glossary /
                          assemble / RunStore

任何下层模块都不得反向导入本模块。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import Config
from .annotations import AnnotationService
from .finalization import AssemblyService, ReportService
from .preparation import PreparationService
from .review_workflow import ReviewService
from .runstore import RunStore
from .runtime import LLMClient, PipelineRuntime, _record_pipeline_metrics, _record_run_metrics
from .translation import TranslationService

ProgressFn = Callable[[int, int, str], None]


class Orchestrator:
    """编排 façade：装配运行时与服务，只保留步骤路由和锁作用域控制。"""

    # 可选步骤 / 连续全流程
    ALL_STEPS = ("translate", "review", "report", "assemble")

    def __init__(self, config: Config, client: LLMClient | None = None):
        """装配共享 runtime 与各领域服务，不做任何领域 I/O。"""
        self.config = config
        self._runtime = PipelineRuntime(config, client=client)
        self.client = self._runtime.client
        self._preparation = PreparationService(self._runtime)
        self._annotations = AnnotationService(self._runtime)
        self._translation = TranslationService(self._runtime, self._annotations)
        self._review = ReviewService(self._runtime)
        self._report = ReportService(self._runtime)
        self._assembly = AssemblyService(self._runtime)

    # ── 公开入口 ──────────────────────────────────────────────────────────
    def prepare(self, input_path: str, *, progress: ProgressFn | None = None) -> RunStore:
        """解析输入并定位状态目录；首次运行时在书级锁内完成初始化。"""
        return self._preparation.prepare(input_path, progress=progress)

    @_record_run_metrics("prepare", ["prepare", "understanding"])
    def prepare_for_translation(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """完成全部译前准备并停止，不翻译正文。

        包括文档解析、语言识别、风格/初始术语分析，以及配置开启时的
        逐章预扫和全书概览。所有阶段均可续跑，再次调用会复用已落盘结果。
        """
        store = self._runtime.measure_stage_call(
            "prepare",
            self._preparation.prepare,
            input_path,
            progress=progress,
        )
        with store.lock():
            self._preparation.activate(store)
            try:
                self._runtime.measure_stage_call(
                    "understanding",
                    self._preparation.ensure_understanding,
                    store,
                    progress=progress,
                )
                self._runtime.log_event(
                    store,
                    "translation_prepared",
                    input_path=input_path,
                    book_understanding=self.config.pipeline.book_understanding,
                )
            finally:
                self._runtime.flush_usage(store, scope="prepare")
            self._runtime.capture_metrics_state(store)
        return store

    @_record_run_metrics(
        "translate",
        ["translate"],
        invocation_fields=("only_chapter",),
    )
    def run(
        self,
        input_path: str,
        *,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """准备运行状态并在书级锁内翻译待处理章节。"""
        store = self._runtime.measure_stage_call(
            "prepare",
            self._preparation.prepare,
            input_path,
            progress=progress,
        )
        with store.lock():
            result = self._run_locked(
                store,
                only_chapter=only_chapter,
                progress=progress,
            )
            self._runtime.capture_metrics_state(store)
            return result

    def _run_locked(
        self,
        store: RunStore,
        *,
        only_chapter: int | None,
        progress: ProgressFn | None,
    ) -> RunStore:
        """恢复语言、校验章节编号、生成全书概览，再委托正文翻译。"""
        manifest = self._preparation.activate(store)
        chapter_indices = {chapter.get("index") for chapter in manifest.get("chapters", [])}
        if only_chapter is not None and only_chapter not in chapter_indices:
            available = sorted(index for index in chapter_indices if isinstance(index, int))
            valid_range = f"0–{available[-1]}" if available else "无可翻译章节"
            raise ValueError(f"章节编号 {only_chapter} 不存在；可用范围：{valid_range}")
        book_synopsis = self._runtime.measure_stage_call(
            "understanding",
            self._preparation.ensure_understanding,
            store,
            progress=progress,
        )
        return self._translation.run(
            store,
            book_synopsis=book_synopsis,
            only_chapter=only_chapter,
            progress=progress,
        )

    @_record_run_metrics("review", ["review"])
    def run_review(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """全量执行只读 Review，并保存正式结果、事件与用量。"""
        store = self._runtime.measure_stage_call(
            "prepare",
            self._preparation.locate_existing,
            input_path,
            progress=progress,
        )
        with store.lock():
            self._preparation.activate(store)
            terms = self._review.session_terms(store)
            outcome = self._runtime.measure_stage_call(
                "review",
                self._review.run_session,
                store,
                terms,
                progress=progress,
            )
            self._runtime.capture_metrics_state(store)
        return {
            "store": store,
            "review_issues": outcome.issues,
            "review_changes": outcome.changes,
            "review_result": outcome.result,
            "review_dir": outcome.run_dir,
        }

    def _run_existing_steps(
        self,
        input_path: str,
        steps: set[str],
        *,
        progress: ProgressFn | None,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """仅从既有状态执行本地收尾阶段，不创建新的翻译任务。"""
        store = self._runtime.measure_stage_call(
            "prepare",
            self._preparation.locate_existing,
            input_path,
            progress=progress,
        )
        with store.lock():
            self._preparation.activate(store)
            result = self._finish_steps_locked(
                store,
                input_path=input_path,
                steps=steps,
                run_steps_input=sorted(steps),
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            self._runtime.capture_metrics_state(store)
            return result

    @_record_run_metrics("report", ["report"])
    def run_report(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """从既有状态重新生成报告，并记录独立运行指标。"""
        return self._run_existing_steps(
            input_path,
            {"report"},
            progress=progress,
        )

    @_record_run_metrics(
        "assemble",
        ["assemble"],
        invocation_fields=("out_format", "pdf_engine"),
    )
    def run_assemble(
        self,
        input_path: str,
        *,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """从既有状态快照导出成品，不等待正在进行的整本翻译。"""
        store = self._runtime.measure_stage_call(
            "prepare",
            self._preparation.locate_existing,
            input_path,
            progress=progress,
        )
        self._runtime.log_event(
            store,
            "run_steps_started",
            steps=["assemble"],
            input_path=input_path,
        )
        outputs = self._assembly.assemble_snapshot(
            store,
            input_path=input_path,
            progress=progress,
            out_format=out_format,
            out_path=out_path,
            pdf_engine=pdf_engine,
        )
        self._runtime.log_event(store, "run_steps_finished", steps=["assemble"], outputs=outputs)
        return {
            "store": store,
            "output": outputs[0] if outputs else None,
            "outputs": outputs,
            "report": None,
            "review_issues": [],
            "review_changes": [],
            "review_result": None,
            "review_dir": None,
        }

    @_record_pipeline_metrics
    def run_steps(
        self,
        input_path: str,
        steps,
        *,
        progress: ProgressFn | None = None,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """按需执行步骤子集（可单选可全选）。steps ⊆ ALL_STEPS。"""
        steps = set(steps)
        run_steps_input = sorted(steps)
        if steps == {"review"}:
            reviewed = self.run_review(input_path, progress=progress)
            return {
                "store": reviewed["store"],
                "output": None,
                "outputs": [],
                "report": None,
                "review_issues": reviewed["review_issues"],
                "review_changes": reviewed["review_changes"],
                "review_result": reviewed["review_result"],
                "review_dir": reviewed["review_dir"],
            }
        if steps == {"assemble"}:
            return self.run_assemble(
                input_path,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
                progress=progress,
            )

        if "translate" in steps:
            store = self.run(input_path, progress=progress)
        else:
            store = self._runtime.measure_stage_call(
                "prepare",
                self._preparation.prepare,
                input_path,
                progress=progress,
            )
            self._preparation.activate(store)
        with store.lock():
            result = self._finish_steps_locked(
                store,
                input_path=input_path,
                steps=steps,
                run_steps_input=run_steps_input,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            self._runtime.capture_metrics_state(store)
            return result

    def _finish_steps_locked(
        self,
        store: RunStore,
        *,
        input_path: str,
        steps: set[str],
        run_steps_input: list[str],
        progress: ProgressFn | None,
        out_format: str,
        out_path: str | None,
        pdf_engine: str,
    ) -> dict[str, Any]:
        """在书级锁内依次委托审校、报告和导出收尾步骤并返回结果汇总。"""
        self._runtime.log_event(
            store,
            "run_steps_started",
            steps=run_steps_input,
            input_path=input_path,
        )

        review_issues: list[dict] = []
        review_changes: list[dict] = []
        review_result: dict[str, Any] | None = None
        review_dir: str | None = None
        report: dict[str, Any] | None = None
        with self._report.glossary_scope(store, "report" in steps) as glossary:
            try:
                if "review" in steps:
                    # 先保存此前阶段的增量，使会话 usage.json 只包含 Review 调用。
                    self._runtime.flush_usage(store, scope="pipeline")
                    terms = self._review.session_terms(store, glossary)
                    outcome = self._runtime.measure_stage_call(
                        "review",
                        self._review.run_session,
                        store,
                        terms,
                        progress=progress,
                    )
                    review_issues = outcome.issues
                    review_changes = outcome.changes
                    review_result = outcome.result
                    review_dir = outcome.run_dir

                self._runtime.flush_usage(store, scope="pipeline")
                if "report" in steps:
                    if glossary is None:  # pragma: no cover - 由 needs 条件保证
                        raise RuntimeError("报告生成需要术语库")
                    report = self._report.build_and_save(
                        store,
                        glossary,
                        progress=progress,
                    )
            finally:
                self._runtime.flush_usage(store, scope="pipeline")

        outputs: list[str] = []
        if "assemble" in steps:
            outputs = self._assembly.assemble_live(
                store,
                input_path=input_path,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )

        self._runtime.log_event(
            store,
            "run_steps_finished",
            steps=run_steps_input,
            outputs=outputs,
        )
        return {
            "store": store,
            "output": outputs[0] if outputs else None,
            "outputs": outputs,
            "report": report,
            "review_issues": review_issues,
            "review_changes": review_changes,
            "review_result": review_result,
            "review_dir": review_dir,
        }

    def run_all(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """翻译 → 最终审校 → 报告 → 回填，返回结果汇总。"""
        steps = {"translate", "report", "assemble"}
        if self.config.pipeline.review:
            steps.add("review")
        return self.run_steps(
            input_path,
            steps,
            progress=progress,
            out_format=out_format,
            out_path=out_path,
            pdf_engine=pdf_engine,
        )
