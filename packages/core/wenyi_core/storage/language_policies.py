"""Backend-neutral immutable language policy artifacts and version checks."""

from __future__ import annotations

import re
from typing import Any, Protocol

from ..i18n.policy.models import PolicyPlan
from ..i18n.policy.registry import OPERATIONS


class PolicyArtifacts(Protocol):
    def read_artifact(self, key: str) -> Any | None: ...
    def write_artifact(self, key: str, value: Any) -> None: ...
    def log_event(self, event: str, **payload: Any) -> None: ...


def persist_plan(store: PolicyArtifacts, plan: PolicyPlan) -> str:
    """Save an immutable artifact and one compact boundary event, without source excerpts."""
    key = f"language-policies/{plan.fingerprint}.json"
    if store.read_artifact(key) is None:
        store.write_artifact(key, plan.to_dict())
    store.log_event(
        "language_policy_resolved",
        phase=plan.context.phase,
        artifact=key,
        fingerprint=plan.fingerprint,
        operations=[op.id for op in plan.operations],
        selections=[
            {"id": op.id, "enabled": op.enabled, "reason": op.reason, "origins": list(op.origins)}
            for op in plan.selections
        ],
    )
    return key


def verify_plan_artifact(store: PolicyArtifacts, reference: Any) -> None:
    """Fail clearly when a checkpoint's pinned implementation is unavailable."""
    if not isinstance(reference, str) or not re.fullmatch(
        r"language-policies/[a-f0-9]{64}\.json", reference
    ):
        raise ValueError("Language policy checkpoint has an invalid plan reference")
    artifact = store.read_artifact(reference)
    if not isinstance(artifact, dict) or artifact.get("schema_version") != 1:
        raise ValueError("Language policy snapshot is missing or invalid")
    if artifact.get("fingerprint") != reference.rsplit("/", 1)[1].removesuffix(".json"):
        raise ValueError("Language policy snapshot fingerprint does not match its reference")
    selections = artifact.get("selections")
    if not isinstance(selections, (list, tuple)):
        raise ValueError("Language policy snapshot has invalid operation selections")
    for selection in selections:
        if not isinstance(selection, dict) or not isinstance(selection.get("enabled"), bool):
            raise ValueError("Language policy snapshot has invalid operation selections")
        if not selection.get("enabled"):
            continue
        operation_id = selection.get("id")
        if not isinstance(operation_id, str):
            raise ValueError("Language policy snapshot has an invalid operation ID")
        spec = OPERATIONS.get(operation_id)
        if (
            spec is None
            or selection.get("implementation_version") != spec.implementation_version
            or selection.get("contract_version") != spec.contract_version
        ):
            raise ValueError(
                f"Pinned language operation is unavailable: {selection.get('id')} "
                f"version {selection.get('implementation_version')}. "
                "Verify the built-in implementation and saved policy artifact."
            )
