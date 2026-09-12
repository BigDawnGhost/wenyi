"""Workflow runner：锁范围 / 节点生命周期 / 必需 vs 尽力而为延续 / 中断恢复。

只依赖 contracts / definition / planner / state / RunStore 面的协议：
不 import 任何具体 Agent、具体节点模块、assemble writer 或 provider 实现，
也不包含翻译业务逻辑（业务全部在具体节点里）。

锁边界：run()/run_goal() 持 store.lock() 进入（即触发 V1 迁移与 running→pending
中断恢复 + 检查点日志恢复）；Application 绝不另行加锁。全新运行在 prepare 暂存
前没有 manifest，prepare/analyze 的生命周期先缓冲在内存，analyze 原子落盘
manifest 后由 runner 合并进 V2 状态（node 状态与 manifest 一起初始化完成）。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from trans_novel.llm.usage_persistence import UsagePersistenceError
from trans_novel.pipeline.contracts import (
    FAILURE_BUSINESS,
    NodeOutcome,
    NodeRequest,
    RunRepository,
    WorkflowNode,
    classify_failure,
)
from trans_novel.pipeline.planning import PlannedStage, WorkflowDefinition, WorkflowPlan
from trans_novel.pipeline.state import (
    NODE_SUCCEEDED,
    SCOPE_CHAPTER,
    STATUS_DONE,
    NodeState,
    RunState,
    now_iso,
)

_CHAPTER_WORKERS = 4  # 共享线程池（润色 + 异步审校复用），硬编码 4，YAGNI


class RequiredNodeFailed(RuntimeError):
    """必需节点失败：计划中止。node 状态已落盘（失败可见）。

    仅用于 provider/protocol 等非业务失败；业务异常（IdentityMismatchError /
    ReadinessError / ValueError 等）在应用边界按原样抛出，保证 CLI 的干净退出路径。
    """


@dataclass
class RunResult:
    plan: WorkflowPlan | None
    outcomes: dict[str, NodeOutcome] = field(default_factory=dict)

    def outcome(self, key: str) -> NodeOutcome | None:
        return self.outcomes.get(key)

    def artifact(self, key: str, name: str, default: Any = None) -> Any:
        outcome = self.outcomes.get(key)
        if outcome is None:
            return default
        return outcome.artifacts.get(name, default)


class WorkflowRunner:
    """执行已规划好的 WorkflowPlan。

    职责边界：
    - 持 store.lock()（进入即触发迁移与中断恢复）；
    - 每个计划项：mark_node_running → execute → succeeded/skipped/failed 落盘；
    - 必需节点失败中止计划（失败状态已落盘）；尽力而为失败记录后继续；
    - 业务异常失败落盘后按原样抛出，不走 RequiredNodeFailed 包装；
    - 成功产物的 artifacts 按节点键并入共享 map，供同轮后续节点消费；
    - 章链收尾：最后一节章节点执行完成后标 done + 发 chapter_done 事件；
    - 收尾阶段事件在计划完成后按用量 scope 检查点发出。
    """

    def __init__(
        self,
        *,
        definition: WorkflowDefinition,
        node_factory: Callable[[str, int | None], WorkflowNode],
        usage_bind: Callable[[RunRepository, str | None], Any] | None = None,
        usage_scope_finish: Callable[[str | None], Any] | None = None,
    ):
        self.definition = definition
        self.node_factory = node_factory
        self.usage_bind = usage_bind
        self.usage_scope_finish = usage_scope_finish

    # ── 入口 ──────────────────────────────────────────────────────────────
    def run(
        self,
        plan_or_builder: WorkflowPlan | Callable[[], WorkflowPlan],
        *,
        store: RunRepository,
        input_path: str,
        progress: Callable[[int, int, str], None] | None = None,
        shared: Any = None,
        usage_scope: str | None = "pipeline",
    ) -> RunResult:
        """执行一个计划；本方法持有运行锁（唯一锁边界）。

        plan_or_builder 传可调用对象时，规划（含指纹对账/附属章升档重开等写
        操作）在锁内执行——Application 绝不另行加锁。
        """
        with store.lock():
            if self.usage_bind is not None:
                self.usage_bind(store, usage_scope)
            try:
                plan = plan_or_builder() if callable(plan_or_builder) else plan_or_builder
                return self._execute_plan(
                    plan,
                    store=store,
                    input_path=input_path,
                    progress=progress,
                    shared=shared,
                    usage_scope=usage_scope,
                )
            finally:
                if self.usage_scope_finish is not None:
                    self.usage_scope_finish(usage_scope)

    def _execute_plan(
        self,
        plan: WorkflowPlan,
        *,
        store: RunRepository,
        input_path: str,
        progress,
        shared: Any,
        usage_scope: str | None,
    ) -> RunResult:
        result = RunResult(plan=plan)
        # 全新运行：prepare 暂存前没有 manifest（计划只含 prepare/analyze）。
        # prepare/analyze 的生命周期先缓冲，analyze 落盘 manifest 后合并进状态。
        self._lifecycle_buffer: dict[str, NodeState] = {}
        state = store.load_state() if store.exists() else RunState()
        total_chapters = len(state.chapters)
        needs_executors = any(
            e.scope == SCOPE_CHAPTER and e.action == "run"
            for stage in plan.stages
            for e in stage.entries
        )
        executor: ThreadPoolExecutor | None = (
            ThreadPoolExecutor(max_workers=_CHAPTER_WORKERS) if needs_executors else None
        )
        artifacts: dict[str, Any] = {}
        try:
            for stage in plan.stages:
                if stage.parallel:
                    self._run_stage_parallel(
                        stage,
                        store=store,
                        input_path=input_path,
                        progress=progress,
                        shared=shared,
                        total_chapters=total_chapters,
                        artifacts=artifacts,
                        executor=executor,
                        result=result,
                    )
                else:
                    for entry in stage.entries:
                        self._run_entry(
                            entry,
                            store=store,
                            input_path=input_path,
                            progress=progress,
                            shared=shared,
                            total_chapters=total_chapters,
                            artifacts=artifacts,
                            executor=executor,
                            result=result,
                        )
        finally:
            if executor is not None:
                executor.shutdown(wait=True)
        return result

    # ── 单条目执行 ────────────────────────────────────────────────────────
    def _run_entry(
        self,
        entry,
        *,
        store: RunRepository,
        input_path: str,
        progress,
        shared: Any,
        total_chapters: int,
        artifacts: dict[str, Any],
        executor,
        result: RunResult,
    ) -> NodeOutcome | None:
        if entry.action == "skip":
            if store.exists():
                store.mark_node_skipped(entry.key)
                if entry.finalize_chapter and entry.ci is not None:
                    self._finalize_chapter(entry.ci, store)
            else:
                self._buffer_skip(entry.key)
            return None
        node = self.node_factory(entry.node_id, entry.ci)
        request = self._request(
            entry,
            store=store,
            input_path=input_path,
            progress=progress,
            shared=shared,
            total_chapters=total_chapters,
            artifacts=artifacts,
            executor=executor,
        )
        self._mark_running(entry.key, store)
        try:
            outcome = node.execute(request)
        except UsagePersistenceError:
            raise
        except Exception as exc:
            self._record_failure(entry, store, exc)
            if self.definition.spec(entry.node_id).failure_policy == "best_effort":
                return None
            if classify_failure(exc) == FAILURE_BUSINESS:
                raise
            raise RequiredNodeFailed(f"节点 {entry.key} 失败：{exc}") from exc
        self._record_success(entry, node, outcome, store)
        result.outcomes[entry.key] = outcome
        artifacts[entry.key] = outcome.artifacts
        if store.exists():
            self._flush_lifecycle(store)
        if entry.finalize_chapter and not outcome.chapter_finalized:
            self._finalize_chapter(entry.ci, store)
        return outcome

    def _run_stage_parallel(
        self,
        stage: PlannedStage,
        *,
        store: RunRepository,
        input_path: str,
        progress,
        shared: Any,
        total_chapters: int,
        artifacts: dict[str, Any],
        executor,
        result: RunResult,
    ) -> None:
        """并行层：worker 线程只跑 node.execute（LLM 调用），全部 RunStore 读写留在
        主线程（仓库既有约定：避免并发读改写丢更新）。"""
        with ThreadPoolExecutor(max_workers=stage.max_workers) as pool:
            futs: dict[Future, Any] = {}
            for entry in stage.entries:
                if entry.action == "skip":
                    if store.exists():
                        store.mark_node_skipped(entry.key)
                    continue
                node = self.node_factory(entry.node_id, entry.ci)
                self._mark_running(entry.key, store)
                request = self._request(
                    entry,
                    store=store,
                    input_path=input_path,
                    progress=progress,
                    shared=shared,
                    total_chapters=total_chapters,
                    artifacts=artifacts,
                    executor=executor,
                )
                futs[pool.submit(node.execute, request)] = (entry, node)
            failures: list[BaseException] = []
            finished: list[tuple[Any, NodeOutcome]] = []
            for fut in as_completed(futs):
                entry, node = futs[fut]
                try:
                    outcome = fut.result()
                except UsagePersistenceError:
                    raise
                except Exception as exc:
                    self._record_failure(entry, store, exc)
                    if self.definition.spec(entry.node_id).failure_policy == "best_effort":
                        continue
                    if classify_failure(exc) == FAILURE_BUSINESS:
                        failures.append(exc)
                    else:
                        failed = RequiredNodeFailed(f"节点 {entry.key} 失败：{exc}")
                        failed.__cause__ = exc
                        failures.append(failed)
                    continue
                finished.append((entry, outcome))
            # 主线程按 key 确定顺序处理：commit（产物落盘）先于成功记录；
            # commit 失败则节点落失败态，并按 execute 异常同等分类处理。
            for entry, outcome in sorted(finished, key=lambda t: t[0].key):
                if outcome.commit is not None:
                    try:
                        commit_request = self._request(
                            entry,
                            store=store,
                            input_path=input_path,
                            progress=progress,
                            shared=shared,
                            total_chapters=total_chapters,
                            artifacts=artifacts,
                            executor=executor,
                        )
                        outcome.commit(commit_request)
                    except UsagePersistenceError:
                        raise
                    except Exception as exc:
                        self._record_failure(entry, store, exc)
                        if self.definition.spec(entry.node_id).failure_policy == "best_effort":
                            continue
                        if classify_failure(exc) == FAILURE_BUSINESS:
                            failures.append(exc)
                        else:
                            failed = RequiredNodeFailed(f"节点 {entry.key} 提交失败：{exc}")
                            failed.__cause__ = exc
                            failures.append(failed)
                        continue
                self._record_success(entry, node, outcome, store)
                result.outcomes[entry.key] = outcome
                artifacts[entry.key] = outcome.artifacts
            if store.exists():
                self._flush_lifecycle(store)
            if failures:
                raise failures[0]

    # ── 生命周期辅助 ──────────────────────────────────────────────────────
    def _mark_running(self, key: str, store: RunRepository) -> None:
        if store.exists():
            store.mark_node_running(key)
            return
        # 全新运行：manifest 落盘前无持久化位置，先缓冲（analyze 落盘后合并）。
        node = self._lifecycle_buffer.get(key)
        if node is None:
            node = NodeState(node_id=key)
            self._lifecycle_buffer[key] = node
        node.status = "running"
        node.attempts += 1
        node.failure = None
        node.started_at = now_iso()
        node.finished_at = None

    def _buffer_skip(self, key: str) -> None:
        node = self._lifecycle_buffer.get(key)
        if node is None:
            node = NodeState(node_id=key)
            self._lifecycle_buffer[key] = node
        node.status = "skipped"
        node.finished_at = now_iso()

    def _flush_lifecycle(self, store: RunRepository) -> None:
        """manifest 出现后把 prepare/analyze 的生命周期合并进 V2 状态。

        只补缺失键与缺失字段：analyze 落盘后已立即持久化 succeeded（attempts=0），
        其 attempts/started_at 必须来自 manifest 落盘前的缓冲记录。
        """
        if not self._lifecycle_buffer:
            return
        state = store.load_state()
        for key, node in self._lifecycle_buffer.items():
            existing = state.nodes.get(key)
            if existing is None:
                state.nodes[key] = node
                continue
            if existing.attempts < node.attempts:
                existing.attempts = node.attempts
            if existing.started_at is None and node.started_at is not None:
                existing.started_at = node.started_at
            if existing.finished_at is None and node.finished_at is not None:
                existing.finished_at = node.finished_at
        store.save_state(state)
        self._lifecycle_buffer = {}

    def _request(
        self,
        entry,
        *,
        store: RunRepository,
        input_path: str,
        progress,
        shared: Any,
        total_chapters: int,
        artifacts: dict[str, Any],
        executor,
    ) -> NodeRequest:
        return NodeRequest(
            store=store,
            node_id=entry.node_id,
            key=entry.key,
            ci=entry.ci,
            scope=entry.scope,
            input_path=input_path,
            progress=progress,
            executor=executor,
            artifacts=dict(artifacts),
            shared=shared,
            finalize_chapter=entry.finalize_chapter,
            total_chapters=total_chapters,
        )

    def _record_success(
        self,
        entry,
        node: WorkflowNode,
        outcome: NodeOutcome,
        store: RunRepository,
    ) -> None:
        if not store.exists():
            self._buffer_success(entry.key, outcome.fingerprint)
            return
        if outcome.fingerprint:
            store.record_node_fingerprint(entry.key, outcome.fingerprint)
        else:
            store.mark_node_succeeded(entry.key)

    def _record_failure(self, entry, store: RunRepository, exc: BaseException) -> None:
        if not store.exists():
            return  # 未初始化运行：失败直接冒泡，续跑整段重来
        kind = classify_failure(exc)
        store.fail_node(entry.key, kind, str(exc))

    def _buffer_success(self, key: str, fingerprint: str | None) -> None:
        node = self._lifecycle_buffer.get(key)
        if node is None:
            node = NodeState(node_id=key)
            self._lifecycle_buffer[key] = node
        node.status = NODE_SUCCEEDED
        node.finished_at = now_iso()
        if fingerprint:
            node.input_fingerprint = fingerprint

    def _finalize_chapter(self, ci: int, store: RunRepository) -> None:
        progress = store.load_progress(ci)
        if progress.status == STATUS_DONE:
            return
        chapter = store.load_chapter(ci)
        event = {
            "chapter": ci,
            "title": chapter.title,
            "segment_count": len(chapter.text_segments),
            "lint_issue_count": len(progress.lint_issues),
        }
        if chapter.processing is not None:
            event["processing"] = chapter.processing.model_dump(mode="json")
        elif progress.back_matter_mode is not None:
            event["back_matter"] = True
            event["mode"] = progress.back_matter_mode
        store.log_event("chapter_done", **event)
        store.set_chapter_status(ci, STATUS_DONE)


__all__ = ["RequiredNodeFailed", "RunResult", "WorkflowRunner"]
