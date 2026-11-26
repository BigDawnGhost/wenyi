"""AssemblyService multi-product export manifest tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from wenyi_core.assemble.writer_common import bilingual_out_path
from wenyi_core.models.errors import ExportPartialError
from wenyi_core.pipeline.finalize.finalization import AssemblyService
from wenyi_core.storage.protocol import Storage

_PLAN = object()


def _service(mono: bool = True, bilingual: bool = True) -> AssemblyService:
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            output=SimpleNamespace(
                mono=mono,
                bilingual=bilingual,
                bilingual_order="target_first",
                bilingual_preserve_source_style=False,
                about_page=True,
            ),
            pipeline=SimpleNamespace(babeldoc_timeout=30),
        )
    )
    return AssemblyService(runtime)  # type: ignore[arg-type]


def _run(service: AssemblyService, tmp_path) -> list[str]:
    return service.assemble_outputs(
        cast(Storage, object()),  # store; only passed to the patched plan/persist helpers
        input_path=str(tmp_path / "src.epub"),
        progress=None,
        out_format="epub",
        out_path=str(tmp_path / "out.epub"),
        policy_store=cast(Storage, object()),
    )


@pytest.fixture()
def export_env(monkeypatch):
    """Patch plan building/persistence and the writer; expose a call recorder."""
    import wenyi_core.assemble.policy as policy_module
    import wenyi_core.assemble.writer as writer_module
    import wenyi_core.pipeline.finalize.finalization as finalization_module

    env = SimpleNamespace(calls=[], results={})

    def fake_export_plan(store, out_format, **kwargs):
        return _PLAN

    def fake_persist_plan(policy_store, plan):
        assert plan is _PLAN

    def fake_assemble(store, input_path, **kwargs):
        env.calls.append(kwargs)
        outcome = env.results[kwargs["bilingual"]]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(policy_module, "export_plan", fake_export_plan)
    monkeypatch.setattr(finalization_module, "persist_plan", fake_persist_plan)
    monkeypatch.setattr(writer_module, "assemble", fake_assemble)
    return env


def test_full_success_returns_both_paths(tmp_path, export_env):
    mono_path = str(tmp_path / "out.epub")
    bilingual_path = str(tmp_path / "out.bilingual.epub")
    export_env.results[False] = mono_path
    export_env.results[True] = bilingual_path

    outputs = _run(_service(), tmp_path)

    assert outputs == [mono_path, bilingual_path]
    assert [call["bilingual"] for call in export_env.calls] == [False, True]


def test_partial_failure_keeps_success_and_reports_manifest(tmp_path, export_env):
    mono_path = str(tmp_path / "out.epub")
    export_env.results[False] = mono_path
    export_env.results[True] = RuntimeError("epub template missing")

    with pytest.raises(ExportPartialError) as excinfo:
        _run(_service(), tmp_path)

    error = excinfo.value
    assert error.code == "export_partial"
    assert error.outputs == [mono_path]
    assert [(item.product, item.ok) for item in error.outcomes] == [
        ("monolingual", True),
        ("bilingual", False),
    ]
    assert error.outcomes[1].error == "epub template missing"
    assert isinstance(error.__cause__, RuntimeError)
    # The failing product must not prevent the remaining product from exporting.
    assert [call["bilingual"] for call in export_env.calls] == [False, True]
    assert export_env.calls[1]["out_path"] == bilingual_out_path(str(tmp_path / "out.epub"))


def test_first_product_failure_still_exports_second(tmp_path, export_env):
    bilingual_path = bilingual_out_path(str(tmp_path / "out.epub"))
    export_env.results[False] = RuntimeError("pdf bridge down")
    export_env.results[True] = bilingual_path

    with pytest.raises(ExportPartialError) as excinfo:
        _run(_service(), tmp_path)

    error = excinfo.value
    assert error.outputs == [bilingual_path]
    assert [call["bilingual"] for call in export_env.calls] == [False, True]
    assert "monolingual epub: failed (pdf bridge down)" in str(error)
    assert f"bilingual epub: success -> {bilingual_path}" in str(error)


def test_all_products_fail_reports_full_manifest(tmp_path, export_env):
    export_env.results[False] = RuntimeError("boom-a")
    export_env.results[True] = RuntimeError("boom-b")

    with pytest.raises(ExportPartialError) as excinfo:
        _run(_service(), tmp_path)

    error = excinfo.value
    assert error.outputs == []
    assert [item.error for item in error.outcomes] == ["boom-a", "boom-b"]
    assert "boom-a" in str(error) and "boom-b" in str(error)


def test_mono_only_success_returns_single_path(tmp_path, export_env):
    mono_path = str(tmp_path / "out.epub")
    export_env.results[False] = mono_path

    outputs = _run(_service(mono=True, bilingual=False), tmp_path)

    assert outputs == [mono_path]
    assert len(export_env.calls) == 1
