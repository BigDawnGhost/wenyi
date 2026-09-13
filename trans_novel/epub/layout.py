"""EPUB 布局分析的不可变数据契约。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

LAYOUT_POLICY_VERSION = "1"
LAYOUT_ROLES = (
    "body",
    "heading",
    "chapter-number",
    "subtitle",
    "caption",
    "quote",
    "quote-text",
    "table-text",
    "list-text",
    "chapter-summary",
    "quote-attribution",
    "footnote",
    "endnote",
    "noteref",
)
_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("layout value must be JSON-compatible") from error


def _validate_json(value: Any) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("layout value must be JSON-compatible")
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("layout value must be JSON-compatible")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_sha256(value: str, field: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase sha256")


def _validate_path(path: tuple[int, ...]) -> None:
    if not isinstance(path, tuple) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in path
    ):
        raise ValueError("path must contain non-negative integers")


def _validate_assignment(role: str | None, level: int | None) -> None:
    if role is not None and role not in LAYOUT_ROLES:
        raise ValueError("invalid layout role")
    if role == "heading":
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise ValueError("heading level must be between 1 and 6")
    elif level is not None:
        raise ValueError("level is only valid for heading")


def source_node_digest(tag: str, attributes: Mapping[str, str], text: str) -> str:
    """摘要原始节点标签、属性和完整 itertext。"""
    if not isinstance(tag, str) or not tag or not isinstance(text, str):
        raise ValueError("source node fields must be strings")
    if not isinstance(attributes, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in attributes.items()
    ):
        raise ValueError("source node attributes must map strings to strings")
    return _digest({"attributes": dict(attributes), "tag": tag, "text": text})


@dataclass(frozen=True, slots=True)
class LayoutNode:
    node_id: str
    resource_href: str
    path: tuple[int, ...]
    source_sha256: str
    group_key: str
    sample: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise ValueError("node_id must be non-empty")
        if not isinstance(self.resource_href, str) or not self.resource_href:
            raise ValueError("resource_href must be non-empty")
        _validate_path(self.path)
        _validate_sha256(self.source_sha256, "source_sha256")
        if not isinstance(self.group_key, str) or not self.group_key:
            raise ValueError("group_key must be non-empty")
        if not isinstance(self.sample, dict):
            raise ValueError("sample must be an object")
        _validate_json(self.sample)


@dataclass(frozen=True, slots=True)
class LayoutInventory:
    source_sha256: str
    nodes: tuple[LayoutNode, ...]
    digest: str
    policy_version: str

    def __post_init__(self) -> None:
        _validate_sha256(self.source_sha256, "source_sha256")
        _validate_sha256(self.digest, "digest")
        if not isinstance(self.nodes, tuple) or not all(
            isinstance(node, LayoutNode) for node in self.nodes
        ):
            raise ValueError("nodes must be LayoutNode tuple")
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("policy_version must be non-empty")
        _validate_unique(self.nodes)


@dataclass(frozen=True, slots=True)
class LayoutAssignment:
    node_id: str
    resource_href: str
    path: tuple[int, ...]
    source_sha256: str
    role: str | None
    level: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise ValueError("node_id must be non-empty")
        if not isinstance(self.resource_href, str) or not self.resource_href:
            raise ValueError("resource_href must be non-empty")
        _validate_path(self.path)
        _validate_sha256(self.source_sha256, "source_sha256")
        _validate_assignment(self.role, self.level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "resource_href": self.resource_href,
            "path": list(self.path),
            "source_sha256": self.source_sha256,
            "role": self.role,
            "level": self.level,
        }


@dataclass(frozen=True, slots=True)
class LayoutProfile:
    source_sha256: str
    inventory_digest: str
    policy_version: str
    assignments: tuple[LayoutAssignment, ...]
    provenance: dict[str, Any]

    def __post_init__(self) -> None:
        _validate_sha256(self.source_sha256, "source_sha256")
        _validate_sha256(self.inventory_digest, "inventory_digest")
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("policy_version must be non-empty")
        if not isinstance(self.assignments, tuple) or not all(
            isinstance(item, LayoutAssignment) for item in self.assignments
        ):
            raise ValueError("assignments must be LayoutAssignment tuple")
        _validate_unique(self.assignments)
        if not isinstance(self.provenance, dict):
            raise ValueError("provenance must be an object")
        _validate_json(self.provenance)

    @property
    def digest(self) -> str:
        return _digest(
            {
                "source_sha256": self.source_sha256,
                "inventory_digest": self.inventory_digest,
                "policy_version": self.policy_version,
                "assignments": [item.to_dict() for item in self.assignments],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "source_sha256": self.source_sha256,
            "inventory_digest": self.inventory_digest,
            "policy_version": self.policy_version,
            "assignments": [item.to_dict() for item in self.assignments],
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LayoutProfile:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "source_sha256",
            "inventory_digest",
            "policy_version",
            "assignments",
            "provenance",
        }:
            raise ValueError("invalid layout profile schema")
        if value["schema_version"] != _SCHEMA_VERSION or isinstance(value["schema_version"], bool):
            raise ValueError("unsupported layout profile schema")
        raw_assignments = value["assignments"]
        if not isinstance(raw_assignments, list):
            raise ValueError("assignments must be an array")
        assignments: list[LayoutAssignment] = []
        fields = {"node_id", "resource_href", "path", "source_sha256", "role", "level"}
        for raw in raw_assignments:
            if not isinstance(raw, dict) or set(raw) != fields:
                raise ValueError("invalid layout assignment schema")
            path = raw["path"]
            if not isinstance(path, list) or any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in path
            ):
                raise ValueError("path must be an integer array")
            assignments.append(
                LayoutAssignment(
                    node_id=raw["node_id"],
                    resource_href=raw["resource_href"],
                    path=tuple(path),
                    source_sha256=raw["source_sha256"],
                    role=raw["role"],
                    level=raw["level"],
                )
            )
        return cls(
            source_sha256=value["source_sha256"],
            inventory_digest=value["inventory_digest"],
            policy_version=value["policy_version"],
            assignments=tuple(assignments),
            provenance=value["provenance"],
        )


def _validate_unique(items: tuple[LayoutNode, ...] | tuple[LayoutAssignment, ...]) -> None:
    ids: set[str] = set()
    locators: set[tuple[str, tuple[int, ...]]] = set()
    for item in items:
        locator = (item.resource_href, item.path)
        if item.node_id in ids or locator in locators:
            raise ValueError("duplicate layout node identity")
        ids.add(item.node_id)
        locators.add(locator)


__all__ = [
    "LAYOUT_POLICY_VERSION",
    "LAYOUT_ROLES",
    "LayoutAssignment",
    "LayoutInventory",
    "LayoutNode",
    "LayoutProfile",
    "source_node_digest",
]
