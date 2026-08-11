"""工作流状态补丁和应用结果的框架无关数据结构。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..domain.workflow import ArtifactRef, WorkflowEvent
from .state import WorkflowState


class InvalidStatePatch(ValueError):
    """补丁结构或补丁应用后的完整状态不合法。"""


class RevisionConflict(InvalidStatePatch):
    """调用方基于过期 revision 计算了新的状态。"""


class OperationConflict(InvalidStatePatch):
    """同一个 operation_id 被用于不同的逻辑操作。"""


class MergeConflict(InvalidStatePatch):
    """并行分支为同一个稳定键返回了互相矛盾的值。"""


@dataclass(frozen=True, slots=True)
class StatePatch:
    """节点对完整顶层切片的原子替换请求。"""

    operation_id: str
    expected_revision: int
    updates: Mapping[str, object]
    events: tuple[WorkflowEvent, ...] = ()
    created_artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True, slots=True)
class PatchApplication:
    """持久化适配器需要提交或幂等补投的完整结果。

    ``duplicate=True`` 只表示状态已提交；同一完整补丁重放时仍返回 effects。
    跨进程冷启动恢复必须依赖 repository 与状态原子提交的完整事件 outbox。
    """

    state: WorkflowState
    events: tuple[WorkflowEvent, ...]
    created_artifacts: tuple[ArtifactRef, ...]
    duplicate: bool


__all__ = [
    "InvalidStatePatch",
    "MergeConflict",
    "OperationConflict",
    "PatchApplication",
    "RevisionConflict",
    "StatePatch",
]
