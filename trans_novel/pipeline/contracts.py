"""Workflow 契约：节点协议、执行目标与失败分类。

这一层只定义形状，不包含任何具体 Agent/节点实现。runner 依赖本模块 +
definition/planner/state，绝不 import 具体节点或 Agent。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.llm.errors import AllModelsFailedError, JSONParseError
from trans_novel.llm.retrying import classify_retry
from trans_novel.pipeline.state import IdentityMismatchError

Scope = Literal["book", "chapter"]
FailurePolicy = Literal["required", "best_effort"]
NodeAction = Literal["run", "skip"]


class ReadinessError(RuntimeError):
    """正式回填被拒：运行身份失配或状态不完整。"""


# 失败 kind 与状态契约一一对应（见 state.NodeFailure）。
FAILURE_PROTOCOL = "protocol"
FAILURE_BUSINESS = "business"
FAILURE_PROVIDER_RETRYABLE = "provider_retryable"
FAILURE_PROVIDER_PERMANENT = "provider_permanent"


def classify_failure(exc: BaseException) -> str:
    """把节点异常归类为稳定的失败 kind。"""
    if isinstance(exc, WorkflowProtocolError | JSONParseError):
        return FAILURE_PROTOCOL
    if isinstance(exc, AllModelsFailedError):
        return FAILURE_PROVIDER_RETRYABLE
    if isinstance(exc, IdentityMismatchError | ReadinessError | ValueError):
        return FAILURE_BUSINESS
    if classify_retry(exc) is not None:
        return FAILURE_PROVIDER_RETRYABLE
    return FAILURE_PROVIDER_PERMANENT


class BatchCommitHook(Protocol):
    """通知 benchmark harness：正文翻译批次已完整持久化。"""

    def after_batch_committed(self, chapter_index: int, start: int, count: int) -> None: ...


ProgressFn = Callable[[int, int, str], None]


class RunRepository(Protocol):
    """runner 依赖的仓库面（组合根注入 RunStore 实现）。

    workflow kernel 只依赖本协议，不 import 具体 RunStore 的文件系统/迁移实现；
    节点仍通过 request.store 使用同一仓库（其完整 API 由节点契约另行承担）。
    """

    def exists(self) -> bool: ...

    def lock(self): ...  # 上下文管理器：进入即触发迁移/中断恢复/检查点恢复

    def load_state(self): ...

    def save_state(self, state) -> None: ...

    def mark_node_running(self, key: str) -> None: ...

    def mark_node_skipped(self, key: str) -> None: ...

    def mark_node_succeeded(self, key: str, fingerprint: str | None = None) -> None: ...

    def record_node_fingerprint(self, key: str, fingerprint: str) -> None: ...

    def fail_node(self, key: str, kind: str, message: str = "") -> None: ...

    def load_progress(self, ci: int): ...

    def save_progress(self, ci: int, progress) -> None: ...

    def load_chapter(self, ci: int): ...

    def log_event(self, event: str, **data: Any) -> None: ...
    def log_event_required(self, event: str, **data: Any) -> None: ...

    def set_chapter_status(self, ci: int, status: str) -> None: ...


@dataclass
class NodeOutcome:
    """单个节点一次执行的结果。成功是默认路径；失败一律用异常表达。"""

    fingerprint: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    findings_count: int = 0
    chapter_finalized: bool = False
    commit: Callable[[NodeRequest], None] | None = None


@dataclass
class NodeRequest:
    """runner 交给节点的一次执行上下文。business 逻辑全部由节点自理。"""

    store: Any
    node_id: str
    key: str
    ci: int | None
    scope: Scope
    input_path: str
    progress: ProgressFn | None = None
    executor: Any = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    shared: Any = None
    finalize_chapter: bool = False
    total_chapters: int = 0


class WorkflowNode(Protocol):
    """具体节点实现协议。节点构造时收到全部精确依赖。"""

    node_id: str
    scope: Scope

    def execute(self, request: NodeRequest) -> NodeOutcome: ...


NodeFactory = Callable[[str, int | None], WorkflowNode]


@dataclass(frozen=True)
class ExecutionGoal:
    """一次运行的执行目标：有序阶段列表 + 章范围/输出参数。"""

    name: str
    phases: tuple[str, ...]
    only_chapter: int | None = None
    out_format: str = "epub"
    out_path: str | None = None
    reanalyze_layout: bool = False


GOAL_PREPARE = ExecutionGoal(name="prepare", phases=("prepare", "prescan"))
GOAL_TRANSLATE = ExecutionGoal(
    name="translate", phases=("prepare", "prescan", "translate", "titles")
)
GOAL_RUN_ALL = ExecutionGoal(
    name="run_all",
    phases=(
        "prepare",
        "layout",
        "prescan",
        "translate",
        "titles",
        "qa",
        "repair",
        "report",
        "assemble",
    ),
)


def translate_chapter_goal(ci: int) -> ExecutionGoal:
    """只翻指定章（调试用，不做收尾）：预扫照常，但只译一章、不译标题。"""
    return ExecutionGoal(
        name=f"translate_chapter:{ci}",
        phases=("prepare", "prescan", "translate"),
        only_chapter=ci,
    )


def qa_goal() -> ExecutionGoal:
    """显式确定性 QA 目标（tools qa / 测试）。"""
    return ExecutionGoal(name="qa", phases=("qa",))


def report_goal() -> ExecutionGoal:
    return ExecutionGoal(name="report", phases=("report",))


def assemble_goal(
    *,
    out_format: str = "epub",
    out_path: str | None = None,
    reanalyze_layout: bool = False,
) -> ExecutionGoal:
    return ExecutionGoal(
        name="assemble",
        phases=("layout", "assemble"),
        out_format=out_format,
        out_path=out_path,
        reanalyze_layout=reanalyze_layout,
    )


def titles_goal() -> ExecutionGoal:
    """仅标题翻译（供独立工具/测试复用 titles 节点）。"""
    return ExecutionGoal(name="titles", phases=("titles",))
