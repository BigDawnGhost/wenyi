"""Deterministic planner for the minimal translation workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from trans_novel.ingest.models import chapter_source_digest
from trans_novel.pipeline.contracts import ExecutionGoal, NodeAction
from trans_novel.pipeline.planning.definition import WorkflowDefinition
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
    NODE_SUCCEEDED,
    NODE_TITLES,
    NODE_TRANSLATE,
    SCOPE_BOOK,
    SCOPE_CHAPTER,
    IdentityMismatchError,
    RunState,
    RunStore,
    chapter_node_key,
)
from trans_novel.pipeline.state.models import TRANSLATION_POLICY_VERSION

_CHAIN = (NODE_TRANSLATE, NODE_POLISH)
_MODEL_WRITING_NODES = frozenset(
    {
        NODE_PREPARE,
        NODE_ANALYZE,
        NODE_MINE_TERMS,
        NODE_NAME_TERMS,
        NODE_TRANSLATE,
        NODE_POLISH,
        NODE_TITLES,
        NODE_REPAIR,
    }
)


@dataclass(frozen=True)
class WorkflowPolicy:
    polish: bool = False
    prescan_concurrency: int = 4

    @classmethod
    def from_config(cls, config) -> WorkflowPolicy:
        p = config.pipeline
        return cls(polish=p.polish, prescan_concurrency=p.prescan_concurrency)


@dataclass
class PrescanInputs:
    mine_fingerprint: Callable[[], str] | None = None
    name_terms_fingerprint: Callable[[], str] | None = None
    prepare_fingerprint: Callable[[], str] | None = None
    analyze_fingerprint: Callable[[], str] | None = None
    layout_fingerprint: Callable[[], str] | None = None
    layout_enabled: bool = False
    layout_profile_valid: bool = False
    translate_fingerprint: Callable[[int], str] | None = None
    polish_fingerprint: Callable[[int], str] | None = None
    titles_fingerprint: Callable[[], str] | None = None
    deterministic_qa_fingerprint: Callable[[], str] | None = None
    report_fingerprint: Callable[[], str] | None = None
    assemble_fingerprint: Callable[[], str] | None = None


@dataclass(frozen=True)
class PlanEntry:
    node_id: str
    key: str
    ci: int | None
    scope: str
    action: NodeAction = "run"
    finalize_chapter: bool = False


@dataclass
class PlannedStage:
    entries: list[PlanEntry]
    parallel: bool = False
    max_workers: int = 1


@dataclass
class WorkflowPlan:
    stages: list[PlannedStage] = field(default_factory=list)
    goal: ExecutionGoal | None = None
    targets: list[int] = field(default_factory=list)

    def entry_keys(self) -> set[str]:
        return {e.key for stage in self.stages for e in stage.entries}


class Planner:
    def __init__(self, definition: WorkflowDefinition):
        self.definition = definition

    def build_plan(
        self,
        *,
        goal: ExecutionGoal,
        store: RunStore,
        policy: WorkflowPolicy,
        prescan: PrescanInputs,
    ) -> WorkflowPlan:
        self.definition.validate_goal(goal.phases)
        state = store.load_state() if store.exists() else RunState()
        legacy = (
            store.exists()
            and state.identity.translation_policy_version != TRANSLATION_POLICY_VERSION
        )
        chapters = list(state.chapters)
        if not legacy:
            self._validate_processing(store, chapters)
        if goal.only_chapter is not None and goal.only_chapter not in {c.index for c in chapters}:
            raise ValueError(f"章节编号 {goal.only_chapter} 不存在")
        if store.exists() and not legacy:
            computed = {}
            for key in state.nodes:
                base, sep, suffix = key.partition(":")
                if base not in self.definition.node_ids:
                    continue
                ci = int(suffix) if sep and suffix.isdigit() else None
                fingerprint = self._fingerprint(base, ci, prescan)
                if fingerprint is not None:
                    computed[key] = fingerprint
            store.reconcile_fingerprints(computed)
            state = store.load_state()
            changed = False
            for key, fingerprint in computed.items():
                node = state.nodes.get(key)
                if (
                    node is not None
                    and node.status == NODE_SUCCEEDED
                    and not node.input_fingerprint
                ):
                    node.input_fingerprint = fingerprint
                    changed = True
            if changed:
                store.save_state(state)
        plan = WorkflowPlan(goal=goal)
        needed: dict[str, PlanEntry] = {}

        def add(node: str, ci: int | None = None, action: NodeAction = "run", final=False):
            key = chapter_node_key(node, ci) if ci is not None else node
            needed.setdefault(
                key,
                PlanEntry(
                    node, key, ci, SCOPE_CHAPTER if ci is not None else SCOPE_BOOK, action, final
                ),
            )

        def need(node: str, ci: int | None = None, *, force: bool = False):
            self._need(node, ci, force, chapters, policy, prescan, state, needed, add)

        if "prepare" in goal.phases:
            need(NODE_PREPARE)
            need(NODE_ANALYZE)
        if "layout" in goal.phases:
            need(NODE_LAYOUT, force=goal.reanalyze_layout)
        if "prescan" in goal.phases:
            need(NODE_MINE_TERMS)
            need(NODE_NAME_TERMS)
        if "translate" in goal.phases:
            targets = (
                [goal.only_chapter]
                if goal.only_chapter is not None
                else sorted(store.pending_chapters())
            )
            plan.targets = [ci for ci in targets if ci is not None]
            for ci in plan.targets:
                need(NODE_TRANSLATE, ci)
                need(NODE_POLISH, ci)
        if "titles" in goal.phases:
            need(NODE_TITLES)
        if "qa" in goal.phases:
            need(NODE_DETERMINISTIC_QA, force=True)
        if "repair" in goal.phases:
            need(NODE_REPAIR, force=True)
        if "report" in goal.phases:
            need(NODE_REPORT, force=True)
        if "assemble" in goal.phases:
            need(NODE_ASSEMBLE, force=True)
        if legacy and any(
            entry.action == "run" and entry.node_id in _MODEL_WRITING_NODES
            for entry in needed.values()
        ):
            raise IdentityMismatchError(
                "旧翻译策略的运行缺少所需结果，不能补译或修复；"
                "请创建新的状态目录重新翻译，原有结果保持不变。"
            )
        self._schedule(plan, needed, chapters, policy)
        return plan

    def _need(self, node, ci, force, chapters, policy, prescan, state, needed, add):
        key = chapter_node_key(node, ci) if ci is not None else node
        if key in needed:
            return
        if node == NODE_LAYOUT and not prescan.layout_enabled:
            add(node, None, "skip")
            return
        chapter = next((c for c in chapters if c.index == ci), None) if ci is not None else None
        if (
            chapter is not None
            and chapter.processing is not None
            and chapter.processing.action == "preserve"
            and node != NODE_TRANSLATE
        ):
            return
        if node == NODE_POLISH and not policy.polish:
            add(node, ci, "skip")
            return
        fn = self._fingerprint(node, ci, prescan)
        current = state.nodes.get(key)
        satisfied = (
            current is not None
            and current.status == NODE_SUCCEEDED
            and (fn is None or not current.input_fingerprint or current.input_fingerprint == fn)
        )
        if node == NODE_LAYOUT and not prescan.layout_profile_valid:
            satisfied = False
        if force or not satisfied:
            add(node, ci)
        if node == NODE_TRANSLATE:
            return
        if node == NODE_LAYOUT:
            self._need(
                NODE_PREPARE,
                None,
                False,
                chapters,
                policy,
                prescan,
                state,
                needed,
                add,
            )
            return
        if node == NODE_POLISH:
            self._need(NODE_TRANSLATE, ci, False, chapters, policy, prescan, state, needed, add)
        elif node == NODE_NAME_TERMS:
            self._need(NODE_ANALYZE, None, False, chapters, policy, prescan, state, needed, add)
            self._need(
                NODE_MINE_TERMS,
                None,
                force or not satisfied,
                chapters,
                policy,
                prescan,
                state,
                needed,
                add,
            )
        elif node == NODE_TITLES:
            for chapter in chapters:
                self._need(
                    NODE_TRANSLATE,
                    chapter.index,
                    False,
                    chapters,
                    policy,
                    prescan,
                    state,
                    needed,
                    add,
                )
                if policy.polish and (
                    chapter.processing is None or chapter.processing.action != "preserve"
                ):
                    self._need(
                        NODE_POLISH,
                        chapter.index,
                        False,
                        chapters,
                        policy,
                        prescan,
                        state,
                        needed,
                        add,
                    )
        elif node == NODE_DETERMINISTIC_QA:
            self._need(NODE_TITLES, None, False, chapters, policy, prescan, state, needed, add)
        elif node == NODE_REPORT:
            repair_force = (
                force and state.identity.translation_policy_version >= TRANSLATION_POLICY_VERSION
            )
            self._need(
                NODE_REPAIR,
                None,
                repair_force,
                chapters,
                policy,
                prescan,
                state,
                needed,
                add,
            )
        elif node == NODE_REPAIR:
            self._need(
                NODE_DETERMINISTIC_QA, None, False, chapters, policy, prescan, state, needed, add
            )
        elif node == NODE_ASSEMBLE:
            self._need(NODE_REPORT, None, False, chapters, policy, prescan, state, needed, add)
            self._need(
                NODE_LAYOUT,
                None,
                False,
                chapters,
                policy,
                prescan,
                state,
                needed,
                add,
            )

    @staticmethod
    def _fingerprint(node: str, ci: int | None, prescan: PrescanInputs) -> str | None:
        """Select the injected fingerprint formula for a current workflow node."""
        callbacks = {
            NODE_PREPARE: prescan.prepare_fingerprint,
            NODE_ANALYZE: prescan.analyze_fingerprint,
            NODE_LAYOUT: prescan.layout_fingerprint,
            NODE_MINE_TERMS: prescan.mine_fingerprint,
            NODE_NAME_TERMS: prescan.name_terms_fingerprint,
            NODE_TRANSLATE: prescan.translate_fingerprint,
            NODE_POLISH: prescan.polish_fingerprint,
            NODE_TITLES: prescan.titles_fingerprint,
            NODE_DETERMINISTIC_QA: prescan.deterministic_qa_fingerprint,
            NODE_REPORT: prescan.report_fingerprint,
            NODE_ASSEMBLE: prescan.assemble_fingerprint,
        }
        callback = callbacks.get(node)
        if callback is None:
            return None
        try:
            return callback(ci) if ci is not None else callback()
        except TypeError:
            return None

    @staticmethod
    def _validate_processing(store, chapters) -> None:
        for chapter_index in chapters:
            chapter = store.load_chapter(chapter_index.index)
            processing = chapter_index.processing
            if chapter.processing != processing:
                raise IdentityMismatchError(
                    f"第{chapter_index.index}章语义处理决策与清单不一致；请创建新的状态目录"
                )
            if (
                processing is not None
                and chapter_source_digest(chapter) != processing.source_sha256
            ):
                raise IdentityMismatchError(
                    f"第{chapter_index.index}章源内容与语义处理决策不一致；请创建新的状态目录"
                )

    def _schedule(self, plan, needed, chapters, policy):
        def take(node, ci=None):
            return needed.get(chapter_node_key(node, ci) if ci is not None else node)

        prep = [take(NODE_PREPARE), take(NODE_ANALYZE)]
        if any(prep):
            plan.stages.append(PlannedStage([x for x in prep if x]))
        layout = take(NODE_LAYOUT)
        if layout is not None:
            plan.stages.append(PlannedStage([layout]))
        mine = take(NODE_MINE_TERMS)
        name = take(NODE_NAME_TERMS)
        if mine is not None:
            plan.stages.append(PlannedStage([mine]))
        if name is not None:
            plan.stages.append(PlannedStage([name]))
        by = {c.index: c for c in chapters}
        entries = []
        for ci in sorted(by):
            chapter_entries = [item for node in _CHAIN if (item := take(node, ci)) is not None]
            if chapter_entries:
                terminal = chapter_entries[-1].key
                entries.extend(
                    PlanEntry(
                        item.node_id,
                        item.key,
                        item.ci,
                        item.scope,
                        item.action,
                        item.key == terminal,
                    )
                    for item in chapter_entries
                )
        if entries:
            plan.stages.append(PlannedStage(entries))
        for node in (NODE_TITLES, NODE_DETERMINISTIC_QA, NODE_REPAIR, NODE_REPORT, NODE_ASSEMBLE):
            item = take(node)
            if item is not None:
                plan.stages.append(PlannedStage([item]))


__all__ = [
    "PlanEntry",
    "PlannedStage",
    "Planner",
    "PrescanInputs",
    "WorkflowPlan",
    "WorkflowPolicy",
]
