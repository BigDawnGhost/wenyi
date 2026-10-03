"""Persist policy identities through Storage without changing saved formal targets."""

from __future__ import annotations

from ..config import Config
from ..storage.language_policies import persist_plan, verify_plan_artifact
from ..storage.protocol import Storage

CHECKPOINT = "language-policies/checkpoint.json"


def initialize_policies(store: Storage, config: Config) -> dict[str, str]:
    references = {
        phase: persist_plan(store, config.language_policy(phase))
        for phase in ("analysis", "translation")
    }
    store.write_artifact(CHECKPOINT, references)
    return references


def translation_revision(store: Storage, config: Config) -> bool:
    """Refresh built-in policies and report whether derived analysis must be rebuilt."""
    checkpoint = store.read_artifact(CHECKPOINT)
    current = {phase: config.language_policy(phase) for phase in ("analysis", "translation")}
    references = {
        phase: f"language-policies/{plan.fingerprint}.json" for phase, plan in current.items()
    }
    changed = not isinstance(checkpoint, dict) or any(
        checkpoint.get(phase) != reference for phase, reference in references.items()
    )
    if not changed:
        for reference in references.values():
            verify_plan_artifact(store, reference)
    for plan in current.values():
        persist_plan(store, plan)
    analysis_changed = (
        not isinstance(checkpoint, dict) or checkpoint.get("analysis") != references["analysis"]
    )
    if not analysis_changed:
        commit_revision(store, config)
    # Commit changed analysis only after it has been rebuilt by the caller.
    return analysis_changed


def commit_revision(store: Storage, config: Config) -> None:
    store.write_artifact(
        CHECKPOINT,
        {
            phase: f"language-policies/{config.language_policy(phase).fingerprint}.json"
            for phase in ("analysis", "translation")
        },
    )
