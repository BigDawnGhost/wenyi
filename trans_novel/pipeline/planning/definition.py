"""Workflow 定义：不可变 NodeSpec 与 WorkflowDefinition + 依赖图校验。

配置只能引用注册过的内置节点 id；不支持任意 YAML DAG、导入路径或插件加载。
校验在首次构建（application 组合根）时执行一次：重复/未知/缺失依赖、环、
非法 book/chapter 作用域边、以及禁用必须节点（在 planner 层按策略校验）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from trans_novel.pipeline.contracts import FailurePolicy, Scope
from trans_novel.pipeline.state import SCOPE_BOOK, SCOPE_CHAPTER

# 聚合边：book 作用域节点依赖 chapter 作用域节点时必须显式声明（fan-in）。


class WorkflowDefinitionError(ValueError):
    """工作流定义/目标不合法（重复/未知/缺失依赖、环、非法作用域边、非法禁用）。"""


@dataclass(frozen=True)
class NodeSpec:
    """一个内置节点的静态描述。"""

    node_id: str
    scope: Scope
    failure_policy: FailurePolicy
    depends_on: tuple[str, ...] = ()
    optional: bool = False
    # book 节点对 chapter 节点的聚合依赖（fan-in）：planner 据此展开到全部章节。
    aggregates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scope not in (SCOPE_BOOK, SCOPE_CHAPTER):
            raise WorkflowDefinitionError(f"节点 {self.node_id}: 非法作用域 {self.scope!r}")
        if self.failure_policy not in ("required", "best_effort"):
            raise WorkflowDefinitionError(
                f"节点 {self.node_id}: 非法失败策略 {self.failure_policy!r}"
            )


class WorkflowDefinition:
    """已注册节点集合：校验通过后不可变。"""

    def __init__(self, specs: Iterable[NodeSpec]):
        self._specs: dict[str, NodeSpec] = {}
        for spec in specs:
            if spec.node_id in self._specs:
                raise WorkflowDefinitionError(f"重复节点 id: {spec.node_id}")
            self._specs[spec.node_id] = spec
        self.validate()

    # ── 查询 ──────────────────────────────────────────────────────────────
    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def spec(self, node_id: str) -> NodeSpec:
        spec = self._specs.get(node_id)
        if spec is None:
            raise WorkflowDefinitionError(f"未注册节点 id: {node_id}")
        return spec

    def has(self, node_id: str) -> bool:
        return node_id in self._specs

    def scope_of(self, node_id: str) -> Scope:
        return self.spec(node_id).scope

    def is_optional(self, node_id: str) -> bool:
        return self.spec(node_id).optional

    def depends_on(self, node_id: str) -> tuple[str, ...]:
        return self.spec(node_id).depends_on

    def aggregates(self, node_id: str) -> tuple[str, ...]:
        return self.spec(node_id).aggregates

    # ── 校验 ──────────────────────────────────────────────────────────────
    def validate(self) -> None:
        """全部注册节点上的静态校验：未知/缺失依赖、环、非法作用域边。"""
        for node_id in self._specs:
            self._validate_node(node_id, visiting=set(), visited=set())

    def _validate_node(self, node_id: str, *, visiting: set[str], visited: set[str]) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise WorkflowDefinitionError(f"依赖环: {' -> '.join([*visiting, node_id])}")
        visiting.add(node_id)
        spec = self._specs[node_id]
        for dep in spec.depends_on:
            if dep not in self._specs:
                raise WorkflowDefinitionError(
                    f"节点 {node_id}: 依赖 {dep!r} 未注册（未知/缺失依赖）"
                )
            dep_spec = self._specs[dep]
            if (
                spec.scope == SCOPE_BOOK
                and dep_spec.scope == SCOPE_CHAPTER
                and dep not in spec.aggregates
            ):
                raise WorkflowDefinitionError(
                    f"节点 {node_id}: book 作用域依赖 chapter 节点 {dep!r} 时"
                    "必须显式声明聚合边（aggregates）"
                )
            self._validate_node(dep, visiting=visiting, visited=visited)
        visiting.remove(node_id)
        visited.add(node_id)

    def validate_disablement(self, disabled: Iterable[str]) -> None:
        """策略禁用的节点必须是已注册的可选节点；必须节点不可禁用。"""
        for node_id in disabled:
            if node_id not in self._specs:
                raise WorkflowDefinitionError(f"禁用未注册节点: {node_id}")
            if not self._specs[node_id].optional:
                raise WorkflowDefinitionError(f"必须节点不可禁用: {node_id}")

    def validate_goal(self, goal_phases: Iterable[str]) -> None:
        known = {
            "prepare",
            "layout",
            "prescan",
            "translate",
            "titles",
            "qa",
            "repair",
            "report",
            "assemble",
        }
        for phase in goal_phases:
            if phase not in known:
                raise WorkflowDefinitionError(f"未知执行阶段: {phase!r}")
