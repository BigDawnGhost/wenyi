"""Validation tests for the four-field LangGraph checkpoint projection."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from trans_novel.graph.state import validate_graph_state

WORKFLOW_ID = "wf-" + "b" * 64


def _valid_state() -> dict[str, object]:
    """Return a mutable valid value so each corruption case stays isolated."""
    return {
        "workflow_id": WORKFLOW_ID,
        "revision": 7,
        "status": "running",
        "phase": "translate_chapters",
    }


def test_validate_graph_state_returns_an_independent_exact_copy() -> None:
    original = _valid_state()

    normalized = validate_graph_state(original)
    original["revision"] = 99

    assert normalized == {
        "workflow_id": WORKFLOW_ID,
        "revision": 7,
        "status": "running",
        "phase": "translate_chapters",
    }
    assert set(normalized) == {"workflow_id", "revision", "status", "phase"}


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("workflow_id", "wf-short"),
        ("workflow_id", "wf-" + "A" * 64),
        ("revision", True),
        ("revision", -1),
        ("status", "cancelled"),
        ("phase", "future_phase"),
    ],
)
def test_validate_graph_state_fails_closed_on_tampered_values(
    field: str,
    invalid: object,
) -> None:
    state = _valid_state()
    state[field] = invalid

    with pytest.raises(ValueError):
        validate_graph_state(state)


@pytest.mark.parametrize("extra", [True, False])
def test_validate_graph_state_requires_exact_keys(extra: bool) -> None:
    state = _valid_state()
    if extra:
        state["payload"] = "must never enter a checkpoint"
    else:
        del state["phase"]

    with pytest.raises(ValueError, match="exactly four"):
        validate_graph_state(state)


def test_validate_graph_state_accepts_read_only_mapping() -> None:
    """The public boundary validates framework mappings without mutating them."""
    mapping: Mapping[str, object] = _valid_state()

    assert validate_graph_state(mapping)["workflow_id"] == WORKFLOW_ID
