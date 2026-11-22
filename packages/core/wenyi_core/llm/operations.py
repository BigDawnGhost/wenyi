"""The single registration point for model-driven workflow operations."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from .operation_names import require_operation_name

if TYPE_CHECKING:
    from ..config import Config


@dataclass(frozen=True)
class OperationSpec:
    """Declare routing and workflow reachability together."""

    id: str
    description: str
    workflows: tuple[str, ...] = ("translate",)
    flags: tuple[str, ...] = ()
    review: bool = False
    resumable_conversation: bool = False
    protocol_version: int = 1


def register_operations(specs: Iterable[OperationSpec]) -> Mapping[str, OperationSpec]:
    """Validate registrations before publishing an immutable registry."""
    registry: dict[str, OperationSpec] = {}
    for spec in specs:
        if not re.fullmatch(r"[a-z][a-z_]*\.[a-z][a-z_]*", spec.id):
            raise ValueError(f"Invalid operation ID: {spec.id}")
        if spec.id in registry:
            raise ValueError(f"Duplicate operation: {spec.id}")
        if spec.protocol_version <= 0:
            raise ValueError(f"Invalid operation protocol version: {spec.id}")
        registry[spec.id] = spec
    return MappingProxyType(registry)


OPERATIONS = register_operations(
    (
        OperationSpec(
            "language.detect",
            "Detect source language",
            workflows=("prepare", "translate"),
            flags=("language_auto",),
        ),
        OperationSpec(
            "analysis.style",
            "Analyze style and character voices",
            workflows=("prepare", "translate"),
        ),
        OperationSpec(
            "synopsis.chapter",
            "Summarize one chapter",
            workflows=("prepare", "translate"),
            flags=("book_understanding",),
        ),
        OperationSpec(
            "synopsis.book",
            "Merge digests into a book synopsis",
            workflows=("prepare", "translate"),
            flags=("book_understanding",),
        ),
        OperationSpec("translation.body", "Translate body paragraphs", protocol_version=2),
        OperationSpec("translation.title", "Translate chapter and TOC titles"),
        OperationSpec(
            "polish.body",
            "Polish translated paragraphs",
            flags=("polish",),
            protocol_version=2,
        ),
        OperationSpec("glossary.extract", "Extract glossary candidates"),
        OperationSpec("glossary.align_history", "Align terms with earlier translations"),
        OperationSpec(
            "glossary.note",
            "Maintain an incremental glossary note",
            flags=("glossary_note_workers",),
        ),
        OperationSpec(
            "annotation.align",
            "Align EPUB annotation positions",
            workflows=("translate", "review"),
            flags=("annotation_alignment",),
        ),
        OperationSpec(
            "review.scan",
            "Review and blindly re-review translated text",
            workflows=("translate", "review"),
            review=True,
            protocol_version=2,
        ),
        OperationSpec(
            "review.verify",
            "Verify issues with evidence",
            workflows=("translate", "review"),
            flags=("review_agent_loop",),
            review=True,
            resumable_conversation=True,
            protocol_version=3,
        ),
        OperationSpec(
            "review.arbitrate",
            "Arbitrate conflicting recommendations",
            workflows=("translate", "review"),
            flags=("review_agent_loop", "review_conflict_arbitration"),
            review=True,
            resumable_conversation=True,
            protocol_version=3,
        ),
        OperationSpec(
            "review.fix",
            "Propose shadow translation replacements",
            workflows=("translate", "review"),
            flags=("review_fix_loop",),
            review=True,
            protocol_version=2,
        ),
        OperationSpec(
            "autofix.verify",
            "Verify issues before publication",
            workflows=("translate", "review"),
            flags=("review_autofix",),
            review=True,
            resumable_conversation=True,
            protocol_version=3,
        ),
        OperationSpec(
            "autofix.fix",
            "Propose replacements for publication",
            workflows=("translate", "review"),
            flags=("review_autofix",),
            review=True,
            protocol_version=2,
        ),
    )
)


def require_operation(operation: str) -> OperationSpec:
    """Reject unknown operations instead of selecting an arbitrary model."""
    require_operation_name(operation)
    return OPERATIONS[operation]


def workflow_operations(workflow: str, flags: Mapping[str, object]) -> tuple[str, ...]:
    """Select reachable operations, including recovery, without touching state or SDKs."""
    if workflow not in {"prepare", "translate", "review"}:
        raise ValueError(f"Unknown model workflow: {workflow}")
    return tuple(
        spec.id
        for spec in OPERATIONS.values()
        if workflow in spec.workflows
        and (not spec.review or workflow == "review" or flags.get("review", True))
        and all(flags.get(flag, True) for flag in spec.flags)
    )


def configured_operations(config: Config, workflow: str) -> tuple[str, ...]:
    """Map product settings to registry flags without constructing a model client."""
    flags = config.pipeline.model_dump()
    flags["language_auto"] = config.source_lang == "auto"
    return workflow_operations(workflow, flags)
