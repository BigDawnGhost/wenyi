"""布局分类的确定性抽样、冲突降级与检查点恢复。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.agents.layout_analyzer import LayoutAnalyzer, LayoutObservation
from trans_novel.epub.layout import (
    LAYOUT_ROLES,
    LayoutAssignment,
    LayoutInventory,
    LayoutNode,
    LayoutProfile,
)

_MAX_OBSERVATIONS = 16
_MAX_REQUEST_CHARS = 24_000
_MAX_EVIDENCE_CHARS = 8_000
_CHECKPOINT_SCHEMA = 1


def _request_size(samples: list[dict[str, Any]]) -> int:
    return len(
        json.dumps(
            {"allowed_roles": list(LAYOUT_ROLES), "samples": samples},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _chunk_sample(node: LayoutNode) -> list[dict[str, Any]]:
    sample = {**node.sample, "node_id": node.node_id}
    evidence_key = next(
        (key for key in ("source_markup", "source_text") if isinstance(sample.get(key), str)),
        None,
    )
    if evidence_key is None or len(sample[evidence_key]) <= _MAX_EVIDENCE_CHARS:
        if _request_size([sample]) > _MAX_REQUEST_CHARS:
            raise WorkflowProtocolError("layout_context_limit")
        return [sample]
    evidence = sample[evidence_key]
    base = {key: value for key, value in sample.items() if key != evidence_key}
    chunks: list[dict[str, Any]] = []
    total = (len(evidence) + _MAX_EVIDENCE_CHARS - 1) // _MAX_EVIDENCE_CHARS
    for index in range(total):
        chunk = {
            **base,
            "node_id": f"{node.node_id}#{index}",
            evidence_key: evidence[index * _MAX_EVIDENCE_CHARS : (index + 1) * _MAX_EVIDENCE_CHARS],
            "evidence_chunk": {"index": index, "total": total},
        }
        if _request_size([chunk]) > _MAX_REQUEST_CHARS:
            raise WorkflowProtocolError("layout_context_limit")
        chunks.append(chunk)
    return chunks


def _checkpoint_state(
    inventory: LayoutInventory, checkpoint: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    supplied = checkpoint if isinstance(checkpoint, dict) else {}
    expected = {
        "schema_version": _CHECKPOINT_SCHEMA,
        "source_sha256": inventory.source_sha256,
        "inventory_digest": inventory.digest,
        "policy_version": inventory.policy_version,
    }
    if any(supplied.get(key) != value for key, value in expected.items()):
        return {}
    raw = supplied.get("observations")
    if not isinstance(raw, dict):
        return {}
    observations: dict[str, dict[str, Any]] = {}
    for node_id, value in raw.items():
        if isinstance(node_id, str) and isinstance(value, dict) and set(value) == {"role", "level"}:
            try:
                _validated_observation(node_id, value["role"], value["level"])
            except ValueError:
                continue
            observations[node_id] = {"role": value["role"], "level": value["level"]}
    return observations


def _validated_observation(node_id: str, role: Any, level: Any) -> LayoutObservation:
    if role is not None and role not in LAYOUT_ROLES:
        raise ValueError("invalid role")
    if role == "heading":
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise ValueError("invalid heading level")
    elif level is not None:
        raise ValueError("invalid non-heading level")
    return LayoutObservation(node_id=node_id, role=role, level=level)


def _save(
    inventory: LayoutInventory,
    observations: dict[str, dict[str, Any]],
    save_checkpoint: Callable[[dict[str, Any]], None],
) -> None:
    save_checkpoint(
        {
            "schema_version": _CHECKPOINT_SCHEMA,
            "source_sha256": inventory.source_sha256,
            "inventory_digest": inventory.digest,
            "policy_version": inventory.policy_version,
            "observations": dict(sorted(observations.items())),
        }
    )


def _batches(samples: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for sample in samples:
        candidate = [*current, sample]
        if current and (
            len(candidate) > _MAX_OBSERVATIONS or _request_size(candidate) > _MAX_REQUEST_CHARS
        ):
            result.append(current)
            current = [sample]
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def _classify_nodes(
    nodes: list[LayoutNode],
    analyzer: LayoutAnalyzer,
    inventory: LayoutInventory,
    cached: dict[str, dict[str, Any]],
    save_checkpoint: Callable[[dict[str, Any]], None],
) -> dict[str, LayoutObservation]:
    chunks_by_node = {node.node_id: _chunk_sample(node) for node in nodes}
    pending = [
        sample
        for node in nodes
        for sample in chunks_by_node[node.node_id]
        if sample["node_id"] not in cached
    ]
    for batch in _batches(pending):
        observations = analyzer.classify(samples=batch, allowed_roles=LAYOUT_ROLES)
        for observation in observations:
            cached[observation.node_id] = {
                "role": observation.role,
                "level": observation.level,
            }
        _save(inventory, cached, save_checkpoint)

    resolved: dict[str, LayoutObservation] = {}
    for node in nodes:
        chunk_ids = [sample["node_id"] for sample in chunks_by_node[node.node_id]]
        parts = [
            _validated_observation(chunk_id, cached[chunk_id]["role"], cached[chunk_id]["level"])
            for chunk_id in chunk_ids
        ]
        decisions = {(part.role, part.level) for part in parts}
        role, level = next(iter(decisions)) if len(decisions) == 1 else (None, None)
        if role is None:
            level = None
        resolved[node.node_id] = LayoutObservation(node_id=node.node_id, role=role, level=level)
    return resolved


def _sample_group(nodes: list[LayoutNode]) -> list[LayoutNode]:
    if len(nodes) <= 6:
        return nodes
    selected: list[LayoutNode] = []

    def add(node: LayoutNode) -> None:
        if node not in selected:
            selected.append(node)

    add(nodes[0])
    add(nodes[-1])
    middle = len(nodes) // 2
    unused_resources = {node.resource_href for node in nodes} - {
        nodes[0].resource_href,
        nodes[-1].resource_href,
    }
    middle_node = next(
        (
            nodes[index]
            for index in sorted(range(len(nodes)), key=lambda index: (abs(index - middle), index))
            if not unused_resources or nodes[index].resource_href in unused_resources
        ),
        nodes[middle],
    )
    add(middle_node)
    for index in (
        len(nodes) // 4,
        (3 * len(nodes)) // 4,
        *range(len(nodes)),
    ):
        add(nodes[index])
        if len(selected) == 6:
            break
    return sorted(selected, key=nodes.index)


def analyze_layout(
    inventory: LayoutInventory,
    analyzer: LayoutAnalyzer,
    *,
    checkpoint: dict[str, Any] | None,
    save_checkpoint: Callable[[dict[str, Any]], None],
) -> LayoutProfile:
    """先抽样验证组一致性；冲突组再逐节点分析并逐批持久化。"""
    cached = _checkpoint_state(inventory, checkpoint)
    groups: dict[str, list[LayoutNode]] = {}
    for node in inventory.nodes:
        groups.setdefault(node.group_key, []).append(node)

    assignments: dict[str, LayoutObservation] = {}
    observed_ids: set[str] = set()
    propagated_ids: set[str] = set()
    for nodes in groups.values():
        sampled = _sample_group(nodes)
        observed = _classify_nodes(
            sampled,
            analyzer,
            inventory,
            cached,
            save_checkpoint,
        )
        observed_ids.update(observed)
        decisions = {(item.role, item.level) for item in observed.values()}
        if len(decisions) == 1 and next(iter(decisions))[0] is not None:
            role, level = next(iter(decisions))
            for node in nodes:
                assignments[node.node_id] = LayoutObservation(
                    node_id=node.node_id, role=role, level=level
                )
            propagated_ids.update(node.node_id for node in nodes if node.node_id not in observed)
            continue
        remaining = [node for node in nodes if node.node_id not in observed]
        assignments.update(observed)
        assignments.update(
            _classify_nodes(
                remaining,
                analyzer,
                inventory,
                cached,
                save_checkpoint,
            )
        )
        observed_ids.update(node.node_id for node in remaining)

    return LayoutProfile(
        source_sha256=inventory.source_sha256,
        inventory_digest=inventory.digest,
        policy_version=inventory.policy_version,
        assignments=tuple(
            LayoutAssignment(
                node_id=node.node_id,
                resource_href=node.resource_href,
                path=node.path,
                source_sha256=node.source_sha256,
                role=assignments[node.node_id].role,
                level=assignments[node.node_id].level,
            )
            for node in inventory.nodes
        ),
        provenance={
            "observed_node_ids": sorted(observed_ids),
            "propagated_node_ids": sorted(propagated_ids),
            "unknown_count": sum(assignment.role is None for assignment in assignments.values()),
        },
    )


__all__ = ["analyze_layout"]
