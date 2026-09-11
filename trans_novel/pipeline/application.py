"""组合根：唯一构造具体 Agent/节点/工作流定义/runner 的生产位置。

- 依赖注入：每个具体节点构造时收到精确依赖（AgentBundle / config / 目标参数）；
- 语言惰性解析：auto 检测后的源语言由 prepare 写入 RunContext；
- 应用门面（Application）暴露 CLI 需要的全部目标与服务：
  prepare / prepare_for_translation / run / run_all / run_goal_result /
  translate_titles / qa / report / assemble / glossary_audit。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from trans_novel.agents.glossary_auditor import GlossaryAuditor
from trans_novel.assemble import preflight_epub
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest import load_document
from trans_novel.ingest.epub.reader import ensure_slot_compatibility
from trans_novel.llm.base import LLMClient
from trans_novel.llm.factory import build_client
from trans_novel.llm.usage_persistence import UsagePersistence
from trans_novel.pipeline.composition import AgentBundle, RunContext, build_node_factory
from trans_novel.pipeline.contracts import (
    GOAL_PREPARE,
    GOAL_RUN_ALL,
    GOAL_TRANSLATE,
    BatchCommitHook,
    ExecutionGoal,
    assemble_goal,
    qa_goal,
    report_goal,
    titles_goal,
    translate_chapter_goal,
)
from trans_novel.pipeline.execution import RunResult, WorkflowRunner
from trans_novel.pipeline.nodes import count_segments
from trans_novel.pipeline.planning import (
    NodeSpec,
    Planner,
    WorkflowDefinition,
    WorkflowPlan,
    WorkflowPolicy,
    build_prescan_inputs,
)
from trans_novel.pipeline.quality import audit_glossary
from trans_novel.pipeline.state import (
    NODE_ANALYZE,
    NODE_ASSEMBLE,
    NODE_DETERMINISTIC_QA,
    NODE_MINE_TERMS,
    NODE_NAME_TERMS,
    NODE_POLISH,
    NODE_PREPARE,
    NODE_REPAIR,
    NODE_REPORT,
    NODE_TITLES,
    NODE_TRANSLATE,
    SCOPE_BOOK,
    SCOPE_CHAPTER,
    RunStore,
    slugify,
)

# 注册的全部内置节点。
_NODE_SPECS = (
    NodeSpec(NODE_PREPARE, SCOPE_BOOK, "required"),
    NodeSpec(NODE_ANALYZE, SCOPE_BOOK, "required", depends_on=(NODE_PREPARE,)),
    NodeSpec(NODE_MINE_TERMS, SCOPE_BOOK, "best_effort", depends_on=(NODE_PREPARE,), optional=True),
    NodeSpec(
        NODE_NAME_TERMS,
        SCOPE_BOOK,
        "best_effort",
        depends_on=(NODE_ANALYZE, NODE_MINE_TERMS),
        optional=True,
    ),
    NodeSpec(NODE_TRANSLATE, SCOPE_CHAPTER, "required", depends_on=(NODE_NAME_TERMS,)),
    NodeSpec(NODE_POLISH, SCOPE_CHAPTER, "required", depends_on=(NODE_TRANSLATE,), optional=True),
    NodeSpec(
        NODE_TITLES,
        SCOPE_BOOK,
        "required",
        depends_on=(NODE_TRANSLATE, NODE_POLISH),
        aggregates=(NODE_TRANSLATE, NODE_POLISH),
    ),
    NodeSpec(NODE_DETERMINISTIC_QA, SCOPE_BOOK, "required", depends_on=(NODE_TITLES,)),
    NodeSpec(NODE_REPAIR, SCOPE_BOOK, "required", depends_on=(NODE_DETERMINISTIC_QA,)),
    NodeSpec(NODE_REPORT, SCOPE_BOOK, "required", depends_on=(NODE_REPAIR,)),
    NodeSpec(NODE_ASSEMBLE, SCOPE_BOOK, "required", depends_on=(NODE_REPORT,)),
)


def build_workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(_NODE_SPECS)


def _preflight_epub_outputs(config, doc, source_path: str, progress) -> None:
    if progress:
        progress(0, 0, "预检 EPUB 输出…")
    modes = [False] if config.output.mono or not config.output.bilingual else []
    if config.output.bilingual:
        modes.append(True)
    for bilingual in modes:
        report = preflight_epub(
            doc,
            source_path,
            bilingual=bilingual,
            order=config.output.bilingual_order,
        )
        failures = report["failures"]
        if failures:
            examples = "；".join(f"{item['code']} ({item['path']})" for item in failures[:3])
            raise ValueError(f"EPUB 输出预检失败：{len(failures)} 项；{examples}")


class Application:
    """工作流应用门面：CLI 的唯一生产入口（组合根）。"""

    def __init__(
        self,
        config: Config,
        client: LLMClient | None = None,
        *,
        frozen_preparation=None,
        batch_commit_hook: BatchCommitHook | None = None,
    ):
        self.config = config
        self.client = client or build_client(config)
        self._usage_persistence = UsagePersistence()
        self.frozen_preparation = frozen_preparation
        self.batch_commit_hook = batch_commit_hook
        self.definition = build_workflow_definition()
        self.planner = Planner(self.definition)

    # ── 目标执行 ──────────────────────────────────────────────────────────
    # 阶段分段：prepare（不依赖暂存章节）→ 翻译闭包（prescan/translate/titles）→
    # 收尾（qa/report/assemble）。章相关规划必须等 prepare 暂存文档后才能进行；
    _PREPARE_PHASES = ("prepare",)
    _TRANSLATE_PHASES = ("prescan", "translate", "titles")
    _FINISH_PHASES = ("qa", "repair", "report", "assemble")

    def run_goal(
        self,
        input_path: str,
        goal: ExecutionGoal,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[RunResult, RunStore]:
        doc = load_document(
            input_path,
            self.config.source_lang,
            self.config.target_lang,
            split_segments=self.config.segment.max_chars_per_segment,
        )
        return self._run_document_goal(doc, input_path, goal, progress=progress)

    def run_document_goal(
        self,
        doc,
        identity_path: str,
        goal: ExecutionGoal,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[RunResult, RunStore]:
        """Run the built-in workflow for an already constructed Document."""
        return self._run_document_goal(doc, identity_path, goal, progress=progress)

    def _run_document_goal(
        self,
        doc,
        identity_path: str,
        goal: ExecutionGoal,
        *,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> tuple[RunResult, RunStore]:
        if (
            doc.fmt == "epub"
            and goal.out_format == "epub"
            and ("assemble" in goal.phases or goal.name == "prepare")
        ):
            _preflight_epub_outputs(self.config, doc, identity_path, progress)
        run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        if store.exists() and doc.fmt == "epub":
            manifest = store.load_manifest()
            if doc.meta.get("epub_sha256") == manifest.get("meta", {}).get("epub_sha256"):
                ensure_slot_compatibility(
                    doc, (store.load_chapter(chapter["index"]) for chapter in manifest["chapters"])
                )
        shared = RunContext(
            store=store,
            config=self.config,
            doc=doc,
            agent_builder=lambda src, tgt: AgentBundle(
                client=self.client, config=self.config, src=src, tgt=tgt
            ),
            frozen_preparation=self.frozen_preparation,
        )
        policy = WorkflowPolicy.from_config(self.config)
        result: RunResult | None = None
        try:
            prep_phases = [p for p in goal.phases if p in self._PREPARE_PHASES]
            if prep_phases:
                prep_goal = ExecutionGoal(name="prepare", phases=tuple(prep_phases))
                result = self._run_plan(
                    store, shared, policy, prep_goal, identity_path, progress, "prepare"
                )

            translate_phases = [p for p in goal.phases if p in self._TRANSLATE_PHASES]
            if translate_phases:
                translate_goal = ExecutionGoal(
                    name=goal.name,
                    phases=tuple(translate_phases),
                    only_chapter=goal.only_chapter,
                    out_format=goal.out_format,
                    out_path=goal.out_path,
                )

                def build_translate_plan():
                    prescan = build_prescan_inputs(
                        self.config, store, policy, shared, translate_goal
                    )
                    plan = self.planner.build_plan(
                        goal=translate_goal, store=store, policy=policy, prescan=prescan
                    )
                    shared.segments_done = 0
                    shared.segments_total = count_segments(store, plan.targets)
                    store.log_event(
                        "translate_run_started",
                        only_chapter=translate_goal.only_chapter,
                        chapters=plan.targets,
                        total_segments=shared.segments_total,
                    )
                    return plan

                result = self._run_plan(
                    store,
                    shared,
                    policy,
                    translate_goal,
                    identity_path,
                    progress,
                    "translate" if "translate" in translate_phases else "prepare",
                    plan_builder=build_translate_plan,
                )
                if "translate" in translate_phases:
                    if progress and shared.segments_total:
                        progress(shared.segments_total, shared.segments_total, "翻译完成")
                    store.log_event("translate_run_finished", total_segments=shared.segments_total)

            finish_phases = [p for p in goal.phases if p in self._FINISH_PHASES]
            if finish_phases:
                finish_goal = ExecutionGoal(
                    name=goal.name,
                    phases=tuple(finish_phases),
                    out_format=goal.out_format,
                    out_path=goal.out_path,
                )
                result = self._run_plan(
                    store, shared, policy, finish_goal, identity_path, progress, "pipeline"
                )
            assert result is not None
            return result, store
        finally:
            shared.close()

    def _run_plan(
        self,
        store: RunStore,
        shared: RunContext,
        policy: WorkflowPolicy,
        goal: ExecutionGoal,
        input_path: str,
        progress,
        usage_scope: str | None,
        plan_builder: Callable[[], WorkflowPlan] | None = None,
    ) -> RunResult:
        """构建并执行一个阶段的计划；运行锁由 runner 持有（唯一锁边界）。

        默认 builder 在锁内调用 build_plan（指纹对账/附属章升档重开必须与执行
        同临界区）；调用方可注入自定义 builder 以夹带锁内书签（进度计数/事件）。
        """

        def build() -> WorkflowPlan:
            prescan = build_prescan_inputs(self.config, store, policy, shared, goal)
            return self.planner.build_plan(goal=goal, store=store, policy=policy, prescan=prescan)

        runner = WorkflowRunner(
            definition=self.definition,
            node_factory=build_node_factory(
                self.client, self.config, shared, goal, self.batch_commit_hook
            ),
            usage_bind=lambda s, scope: self.bind_usage(s, scope=scope),
            usage_scope_finish=self.finish_usage,
        )
        return runner.run(
            plan_builder or build,
            store=store,
            input_path=input_path,
            progress=progress,
            shared=shared,
            usage_scope=usage_scope,
        )

    @staticmethod
    def _usage_scope(goal: ExecutionGoal) -> str | None:
        if "translate" in goal.phases:
            return "translate"
        if any(p in goal.phases for p in ("qa", "report", "assemble")):
            return "pipeline"
        if "prepare" in goal.phases or "prescan" in goal.phases:
            return "prepare"
        return None

    # ── CLI 目标 ──────────────────────────────────────────────────────────
    def prepare(self, input_path: str, *, progress=None) -> RunStore:
        """解析 + 初始化 + 风格分析（不预扫、不翻译）。"""
        _, store = self.run_goal(
            input_path, ExecutionGoal(name="prepare", phases=("prepare",)), progress=progress
        )
        return store

    def prepare_for_translation(self, input_path: str, *, progress=None) -> RunStore:
        """完成文档解析、全书预扫和术语定名，但不翻译正文。"""
        _, store = self.run_goal(input_path, GOAL_PREPARE, progress=progress)
        return store

    def run(
        self,
        input_path: str,
        *,
        only_chapter: int | None = None,
        progress=None,
    ) -> RunStore:
        """翻译（only_chapter 时只译一章，不做收尾）。"""
        goal = translate_chapter_goal(only_chapter) if only_chapter is not None else GOAL_TRANSLATE
        _, store = self.run_goal(input_path, goal, progress=progress)
        return store

    def run_all(
        self,
        input_path: str,
        *,
        progress=None,
        out_format: str = "epub",
        out_path: str | None = None,
    ) -> dict[str, Any]:
        """翻译 → 确定性 QA → 报告 → 回填，返回结果汇总。"""
        goal = ExecutionGoal(
            name="run_all",
            phases=GOAL_RUN_ALL.phases,
            out_format=out_format,
            out_path=out_path,
        )
        return self._steps_result(input_path, goal, progress=progress)

    def run_goal_result(
        self,
        input_path: str,
        goal: ExecutionGoal,
        *,
        progress=None,
    ) -> dict[str, Any]:
        return self._steps_result(input_path, goal, progress=progress)

    def _steps_result(self, input_path: str, goal: ExecutionGoal, *, progress=None) -> dict:
        result, store = self.run_goal(input_path, goal, progress=progress)
        outputs = result.artifact("assemble", "outputs", [])
        report = result.artifact("report", "report") or {}
        return {
            "store": store,
            "output": outputs[0] if outputs else None,
            "outputs": outputs,
            "report": report,
            "qa_issues": report.get("deterministic_issues", []),
        }

    def translate_titles(self, store: RunStore) -> None:
        """仅翻译章标题/目录项（独立工具/测试复用 titles 节点）。"""
        self._service_goal(store, titles_goal())

    def qa(self, store: RunStore) -> list[dict]:
        result = self._service_goal(store, qa_goal())
        return result.artifact("deterministic_qa", "issues", [])

    def report(self, store: RunStore) -> dict:
        result = self._service_goal(store, report_goal())
        return result.artifact("report", "report")

    def assemble(
        self,
        store: RunStore,
        input_path: str,
        *,
        out_format: str = "epub",
        out_path: str | None = None,
        mono: bool | None = None,
        bilingual: bool | None = None,
    ) -> list[str]:
        """对已有状态回填（tools assemble；输出开关按 CLI flag 覆盖）。"""
        old_mono, old_bi = self.config.output.mono, self.config.output.bilingual
        if mono is not None:
            self.config.output.mono = mono
        if bilingual is not None:
            self.config.output.bilingual = bilingual
        try:
            goal = assemble_goal(out_format=out_format, out_path=out_path)
            result = self._service_goal(store, goal, input_path=input_path)
            return result.artifact("assemble", "outputs", [])
        finally:
            self.config.output.mono, self.config.output.bilingual = old_mono, old_bi

    def glossary_audit(self, store: RunStore) -> list[dict]:
        with store.lock():
            self.bind_usage(store, scope=None)
            glossary = GlossaryStore(store.glossary_path)
            try:
                return audit_glossary(store, glossary, GlossaryAuditor(self.client, self.config))
            finally:
                glossary.close()

    # ── 服务目标（复用 runner 跑单阶段计划）────────────────────────────────
    def _service_goal(
        self,
        store: RunStore,
        goal: ExecutionGoal,
        *,
        input_path: str | None = None,
    ) -> RunResult:
        shared = RunContext(
            store=store,
            config=self.config,
            doc=None,
            agent_builder=lambda src, tgt: AgentBundle(
                client=self.client, config=self.config, src=src, tgt=tgt
            ),
        )
        policy = WorkflowPolicy.from_config(self.config)
        try:
            runner = WorkflowRunner(
                definition=self.definition,
                node_factory=build_node_factory(
                    self.client, self.config, shared, goal, self.batch_commit_hook
                ),
                usage_bind=lambda s, scope: self.bind_usage(s, scope=scope),
                usage_scope_finish=self.finish_usage,
            )

            def build() -> WorkflowPlan:
                prescan = build_prescan_inputs(self.config, store, policy, shared, goal)
                return self.planner.build_plan(
                    goal=goal, store=store, policy=policy, prescan=prescan
                )

            source_path = input_path or store.load_state().source_path or ""
            return runner.run(
                build,
                store=store,
                input_path=source_path,
                progress=None,
                shared=shared,
                usage_scope=self._usage_scope(goal),
            )
        finally:
            shared.close()

    # ── 用量持久化绑定与阶段事件 ──────────────────────────────────────────
    def bind_usage(self, store: RunStore, *, scope: str | None) -> None:
        self._usage_persistence.bind(
            store.usage_path,
            self.client.usage,
            event_callback=lambda event_scope, increment: store.log_event(
                "usage_summary",
                scope=event_scope,
                increment=increment,
            ),
        )
        self._usage_persistence.begin_scope(scope)

    def finish_usage(self, scope: str | None) -> None:
        self._usage_persistence.finish_scope(scope)


__all__ = ["Application", "build_workflow_definition"]
