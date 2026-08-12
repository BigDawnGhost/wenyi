"""Legacy pipeline runtime adapter tests; no translation stages are executed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runtime import SourceIdentityRuntime


def test_source_identity_falls_back_to_hasher_without_invocation_metrics() -> None:
    hasher = Mock(return_value="a" * 64)
    runtime = SourceIdentityRuntime(hasher)

    assert runtime.verified_sha256("book.txt", recorder=None) == "a" * 64
    assert runtime.initial_sha256("book.txt", recorder=None) == "a" * 64
    assert hasher.call_count == 2
    hasher.assert_called_with("book.txt")


def test_source_identity_uses_only_the_current_invocation_recorder() -> None:
    hasher = Mock(side_effect=["fallback-first", "fallback-second"])
    runtime = SourceIdentityRuntime(hasher)
    first = SimpleNamespace(
        input={"sha256": "first-startup"},
        verify_input_sha256=Mock(return_value="first-verified"),
    )
    second = SimpleNamespace(
        input={"sha256": "second-startup"},
        verify_input_sha256=Mock(return_value=None),
    )

    assert runtime.initial_sha256("first.txt", recorder=first) == "first-startup"
    assert runtime.verified_sha256("first.txt", recorder=first) == "first-verified"
    assert runtime.initial_sha256("second.txt", recorder=second) == "second-startup"
    assert runtime.verified_sha256("second.txt", recorder=second) == "fallback-first"

    assert first.input == {"sha256": "first-startup"}
    assert second.input == {"sha256": "fallback-first"}
    first.verify_input_sha256.assert_called_once_with("first.txt")
    second.verify_input_sha256.assert_called_once_with("second.txt")


def test_store_validation_preserves_the_orchestrator_hash_override_seam() -> None:
    """Store checks must honor subclasses or integrations overriding the legacy hook."""
    # Bypass ``__init__`` so this compatibility test does not construct agents or
    # execute any translation module; only the historical helper chain is exercised.
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._source_sha256 = Mock(return_value="b" * 64)
    store = Mock()
    store.ensure_source_identity.return_value = "b" * 64

    assert orchestrator._ensure_store_source(store, "book.txt") == "b" * 64
    orchestrator._source_sha256.assert_called_once_with("book.txt")
    store.ensure_source_identity.assert_called_once_with(
        "book.txt",
        actual_sha256="b" * 64,
    )
