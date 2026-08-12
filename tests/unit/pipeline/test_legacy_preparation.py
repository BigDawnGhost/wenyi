from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from trans_novel.pipeline.preparation import LegacyPreparationPort


def test_legacy_port_preserves_the_private_sample_override_seam() -> None:
    """风格采样必须经过旧 host，不能被应用协调器改成静态直调。"""
    host = SimpleNamespace(_sample_text=Mock(return_value="overridden sample"))
    document = object()
    port = LegacyPreparationPort(host, Mock())

    assert port.sample_text(document) == "overridden sample"
    host._sample_text.assert_called_once_with(document)


def test_legacy_port_preserves_source_identity_override_seams() -> None:
    """准备协调器继续调用旧实例上的初始摘要和当前摘要钩子。"""
    host = SimpleNamespace(
        _initial_source_sha256=Mock(return_value="a" * 64),
        _source_sha256=Mock(return_value="b" * 64),
        _ensure_store_source=Mock(return_value="b" * 64),
    )
    store = object()
    port = LegacyPreparationPort(host, Mock())

    assert port.initial_source_hash("book.txt") == "a" * 64
    assert port.verified_source_hash("book.txt") == "b" * 64
    assert port.ensure_state_source(store, "book.txt") == "b" * 64
    host._initial_source_sha256.assert_called_once_with("book.txt")
    host._source_sha256.assert_called_once_with("book.txt")
    host._ensure_store_source.assert_called_once_with(store, "book.txt")


def test_bind_state_binds_llm_events_before_attaching_metrics() -> None:
    """The event sink must be ready before later calls can emit measured activity."""
    calls: list[tuple[object, ...]] = []
    host = SimpleNamespace(
        _bind_llm_events=Mock(
            side_effect=lambda store, progress: calls.append(("events", store, progress))
        ),
        _attach_metrics_store=Mock(side_effect=lambda store: calls.append(("metrics", store))),
    )
    store = object()
    progress = Mock()

    LegacyPreparationPort(host, Mock()).bind_state(store, progress)  # type: ignore[arg-type]

    assert calls == [
        ("events", store, progress),
        ("metrics", store),
    ]
