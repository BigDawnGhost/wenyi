"""组合根：构造具体 Agent、节点、工作流与 CLI 应用门面。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from trans_novel.agents.glossary_auditor import GlossaryAuditor
from trans_novel.assemble import preflight_epub
from trans_novel.assemble.epub.rendering.theme import resolve_theme, semantic_output_digest
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.ingest import load_document
from trans_novel.llm.base import LLMClient
from trans_novel.llm.factory import build_client
from trans_novel.llm.usage_persistence import UsagePersistence
from trans_novel.pipeline.application_notes import ensure_document_notes, ensure_service_notes
from trans_novel.pipeline.composition import AgentBundle, RunContext, build_node_factory
from trans_novel.pipeline.composition.output import load_effective_output, save_effective_output
from trans_novel.pipeline.contracts import (
    GOAL_PREPARE,
    GOAL_RUN_ALL,
    GOAL_TRANSLATE,
    BatchCommitHook,
    ExecutionGoal,
    ProgressFn,
    assemble_goal,
    qa_goal,
    report_goal,
    titles_goal,
    translate_chapter_goal,
)
from trans_novel.pipeline.execution import RunResult, WorkflowRunner, assemble_readiness_problems
from trans_novel.pipeline.nodes import count_segments, current_layout_state
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
    NODE_LAYOUT,
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
    IdentityMismatchError,
    RunStore,
    normalize_lang_code,
    slugify,
)
from trans_novel.pipeline.state.models import TRANSLATION_POLICY_VERSION

# 注册的全部内置节点。
_NODE_SPECS = (
    NodeSpec(NODE_PREPARE, SCOPE_BOOK, "required"),
    NodeSpec(NODE_ANALYZE, SCOPE_BOOK, "required", depends_on=(NODE_PREPARE,)),
    NodeSpec(NODE_LAYOUT, SCOPE_BOOK, "required", depends_on=(NODE_PREPARE,)),
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
    NodeSpec(
        NODE_ASSEMBLE,
        SCOPE_BOOK,
        "required",
        depends_on=(NODE_REPORT, NODE_LAYOUT),
    ),
)


def build_workflow_definition() -> WorkflowDefinition:
    return WorkflowDefinition(_NODE_SPECS)


_COMPLETED_LEGACY_OUTPUT = "completed_legacy_output"
_OUTPUT_MAINTENANCE_PHASES = ("layout", "report", "assemble")


def _adapt_completed_legacy_goal(
    store: RunStore, goal: ExecutionGoal, *, frozen_preparation
) -> ExecutionGoal:
    if (
        not store.exists()
        or goal.phases != GOAL_RUN_ALL.phases
        or goal.only_chapter is not None
        or frozen_preparation is not None
    ):
        return goal
    state = store.load_state()
    if state.identity.translation_policy_version >= TRANSLATION_POLICY_VERSION:
        return goal
    return replace(goal, name=_COMPLETED_LEGACY_OUTPUT, phases=_OUTPUT_MAINTENANCE_PHASES)


def _ensure_completed_legacy_output_ready(store: RunStore) -> None:
    state = store.load_state()
    if state.identity.translation_policy_version >= TRANSLATION_POLICY_VERSION:
        raise IdentityMismatchError("旧翻译策略输出续跑条件已变化；请重新运行命令。")
    problems = assemble_readiness_problems(store, require_output_nodes=False)
    if problems:
        raise IdentityMismatchError(
            "旧翻译策略的运行缺少所需结果，不能补译或修复；"
            "请创建新的状态目录重新翻译，原有结果保持不变。"
        )


def _preflight_epub_outputs(
    output,
    doc,
    source_path: str,
    progress,
    theme,
    source_lang: str,
    target_lang: str,
    max_chars: int,
) -> None:
    doc = doc or load_document(source_path, source_lang, target_lang, split_segments=max_chars)
    if progress:
        progress(0, 0, "预检 EPUB 输出…")
    modes = [False] if output.mono else []
    if output.bilingual.enabled:
        modes.append(True)
    for bilingual in modes:
        report = preflight_epub(
            doc,
            source_path,
            bilingual=bilingual,
            order=output.bilingual.order,
            theme=theme,
        )
        failures = report["failures"]
        if failures:
            examples = "；".join(f"{item['code']} ({item['path']})" for item in failures[:3])
            raise ValueError(f"EPUB 输出预检失败：{len(failures)} 项；{examples}")


def _setup_output(config: Config, shared: RunContext, input_path: str, progress=None) -> None:
    """冻结静态输出资源；EPUB profile 相关校验留到 layout 之后。"""
    if not shared.output_relevant or shared.output_ready:
        return
    output, origins, normalized, source_sha = load_effective_output(
        shared.store,
        shared.output,
        config.output_origins,
        input_path=input_path,
        identity_languages=shared.identity_languages,
        out_format=shared.output_format,
    )
    bundle = None
    if shared.output_format == "epub":
        override = output.override_theme
        bundle = resolve_theme(
            override.styles if override is not None else None,
            output.bilingual_styles if output.bilingual.enabled else None,
            origins=origins,
        )
        static_theme = ThemeService(bundle)
        source_format = getattr(shared.doc, "fmt", None)
        if source_format is None and shared.store.exists():
            source_format = shared.store.load_state().fmt
        if source_format == "epub":
            static_theme.preflight_source(input_path)
        _preflight_epub_outputs(
            output,
            shared.doc,
            input_path,
            progress,
            None,
            *shared.identity_languages,
            config.segment.max_chars_per_segment,
        )
    save_effective_output(
        shared.store,
        output,
        origins,
        source_sha=source_sha,
        normalized=normalized,
    )
    if shared.output_format == "txt":
        if output.override_theme is not None or output.bilingual.enabled:
            shared.store.log_event("epub_presentation_inapplicable", out_format="txt")
        shared.output_digest = semantic_output_digest(
            None,
            out_format="txt",
            mono=output.mono,
            bilingual=output.bilingual.enabled,
            bilingual_order=output.bilingual.order,
        )
    shared.output = output
    shared.theme_bundle = bundle
    shared.output_ready = True


def _setup_layout(
    shared: RunContext,
    input_path: str,
    progress: ProgressFn | None = None,
    *,
    notify_reuse: bool = False,
) -> None:
    bundle = shared.theme_bundle
    if (
        shared.output_format != "epub"
        or bundle is None
        or bundle.general_css is None
        or shared.layout_inventory is not None
    ):
        return
    shared.layout_inventory, shared.layout_profile = current_layout_state(shared.store, input_path)
    if shared.layout_profile is not None and notify_reuse:
        shared.store.log_event(
            "layout_profile_reused",
            profile_digest=shared.layout_profile.digest,
        )
        if progress:
            progress(0, 0, "复用已保存的 EPUB 排版分析，不调用模型")


def _setup_rendering(config: Config, shared: RunContext, input_path: str, progress) -> None:
    if shared.output_digest is not None:
        return
    bundle = shared.theme_bundle
    if shared.output_format != "epub" or bundle is None:
        return
    theme = ThemeService(bundle, layout=shared.layout_profile)
    _preflight_epub_outputs(
        shared.output,
        shared.doc,
        input_path,
        progress,
        theme,
        *shared.identity_languages,
        config.segment.max_chars_per_segment,
    )
    shared.theme = theme
    shared.output_digest = semantic_output_digest(
        bundle,
        out_format="epub",
        mono=shared.output.mono,
        bilingual=shared.output.bilingual.enabled,
        bilingual_order=shared.output.bilingual.order,
        layout_digest=(shared.layout_profile.digest if shared.layout_profile is not None else None),
    )


def _run_service_goal(
    application,
    store: RunStore,
    goal: ExecutionGoal,
    *,
    input_path: str | None = None,
    output=None,
    progress: ProgressFn | None = None,
) -> RunResult:
    ensure_service_notes(application, store, goal, input_path, output)
    if "layout" in goal.phases and len(goal.phases) > 1:
        layout_goal = ExecutionGoal(
            name=goal.name,
            phases=("layout",),
            out_format=goal.out_format,
            out_path=goal.out_path,
            reanalyze_layout=goal.reanalyze_layout,
        )
        application._service_goal(
            store,
            layout_goal,
            input_path=input_path,
            output=output,
            progress=progress,
        )
        remaining = ExecutionGoal(
            name=goal.name,
            phases=tuple(phase for phase in goal.phases if phase != "layout"),
            out_format=goal.out_format,
            out_path=goal.out_path,
        )
        return application._service_goal(
            store,
            remaining,
            input_path=input_path,
            output=output,
            progress=progress,
        )
    source_path = input_path or store.load_state().source_path or ""
    identity = store.load_state().identity
    shared = RunContext(
        store=store,
        config=application.config,
        doc=None,
        agent_builder=lambda src, tgt: AgentBundle(
            client=application.client, config=application.config, src=src, tgt=tgt
        ),
        output=output if output is not None else application.config.output.model_copy(deep=True),
        output_format=goal.out_format,
        output_relevant=(
            "layout" in goal.phases or "assemble" in goal.phases or goal.name == "prepare"
        ),
        identity_languages=(identity.source_lang, identity.target_lang),
    )
    policy = WorkflowPolicy.from_config(application.config)
    try:
        runner = WorkflowRunner(
            definition=application.definition,
            node_factory=build_node_factory(
                application.client,
                application.config,
                shared,
                goal,
                application.batch_commit_hook,
            ),
            usage_bind=lambda current_store, scope: application.bind_usage(
                current_store, scope=scope
            ),
            usage_scope_finish=application.finish_usage,
        )

        def build() -> WorkflowPlan:
            _setup_output(application.config, shared, source_path)
            if "layout" in goal.phases or "assemble" in goal.phases:
                _setup_layout(
                    shared,
                    source_path,
                    progress,
                    notify_reuse="layout" in goal.phases and not goal.reanalyze_layout,
                )
            if "assemble" in goal.phases:
                _setup_rendering(application.config, shared, source_path, progress)
            prescan = build_prescan_inputs(application.config, store, policy, shared, goal)
            return application.planner.build_plan(
                goal=goal,
                store=store,
                policy=policy,
                prescan=prescan,
            )

        return runner.run(
            build,
            store=store,
            input_path=source_path,
            progress=progress,
            shared=shared,
            usage_scope=application._usage_scope(goal),
        )
    finally:
        shared.close()


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

    # 阶段分段：prepare → layout → 翻译闭包 → 收尾。layout 的执行顺序早于正文翻译，
    # 但依赖图只通向 assemble，更换排版不会触发正文重新翻译。
    _PREPARE_PHASES = ("prepare",)
    _LAYOUT_PHASES = ("layout",)
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
        run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        original_goal = goal
        shared = RunContext(
            store=store,
            config=self.config,
            doc=doc,
            agent_builder=lambda src, tgt: AgentBundle(
                client=self.client, config=self.config, src=src, tgt=tgt
            ),
            frozen_preparation=self.frozen_preparation,
            output=self.config.output.model_copy(deep=True),
            output_format=goal.out_format,
            output_relevant=(
                "layout" in goal.phases or "assemble" in goal.phases or goal.name == "prepare"
            ),
            identity_languages=(
                normalize_lang_code(self.config.source_lang),
                normalize_lang_code(self.config.target_lang),
            ),
        )
        policy = WorkflowPolicy.from_config(self.config)
        result: RunResult | None = None
        try:
            ensure_document_notes(self, store, doc, identity_path, original_goal, shared)
            goal = _adapt_completed_legacy_goal(
                store, original_goal, frozen_preparation=self.frozen_preparation
            )
            prep_phases = [p for p in goal.phases if p in self._PREPARE_PHASES]
            if prep_phases:
                prep_goal = ExecutionGoal(name="prepare", phases=tuple(prep_phases))
                result = self._run_plan(
                    store, shared, policy, prep_goal, identity_path, progress, "prepare"
                )

            layout_phases = [p for p in goal.phases if p in self._LAYOUT_PHASES]
            if layout_phases:
                layout_goal = ExecutionGoal(
                    name=goal.name,
                    phases=tuple(layout_phases),
                    out_format=goal.out_format,
                    out_path=goal.out_path,
                    reanalyze_layout=goal.reanalyze_layout,
                )
                result = self._run_plan(
                    store, shared, policy, layout_goal, identity_path, progress, "layout"
                )

            translate_phases = [p for p in goal.phases if p in self._TRANSLATE_PHASES]
            if translate_phases:
                translate_goal = ExecutionGoal(
                    name=goal.name,
                    phases=tuple(translate_phases),
                    only_chapter=goal.only_chapter,
                    out_format=goal.out_format,
                    out_path=goal.out_path,
                    reanalyze_layout=goal.reanalyze_layout,
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
                    reanalyze_layout=goal.reanalyze_layout,
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
            _setup_output(self.config, shared, input_path, progress)
            if goal.name == _COMPLETED_LEGACY_OUTPUT:
                _ensure_completed_legacy_output_ready(store)
            if "layout" in goal.phases or "assemble" in goal.phases:
                _setup_layout(
                    shared,
                    input_path,
                    progress,
                    notify_reuse="layout" in goal.phases and not goal.reanalyze_layout,
                )
            if "assemble" in goal.phases:
                _setup_rendering(self.config, shared, input_path, progress)
            if plan_builder is not None:
                return plan_builder()
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
            build,
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
        if "layout" in goal.phases:
            return "layout"
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
            "output_digest": result.artifact("assemble", "output_digest"),
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
        reanalyze_layout: bool = False,
        progress: ProgressFn | None = None,
    ) -> list[str]:
        """对已有状态回填（tools assemble；输出开关按 CLI flag 覆盖）。"""
        output = self.config.output.model_copy(deep=True)
        if mono is not None:
            output = output.model_copy(update={"mono": mono})
        if bilingual is not None:
            output = output.model_copy(
                update={"bilingual": output.bilingual.model_copy(update={"enabled": bilingual})}
            )
        goal = assemble_goal(
            out_format=out_format,
            out_path=out_path,
            reanalyze_layout=reanalyze_layout,
        )
        result = self._service_goal(
            store,
            goal,
            input_path=input_path,
            output=output,
            progress=progress,
        )
        return result.artifact("assemble", "outputs", [])

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
        output=None,
        progress: ProgressFn | None = None,
    ) -> RunResult:
        return _run_service_goal(
            self,
            store,
            goal,
            input_path=input_path,
            output=output,
            progress=progress,
        )

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
