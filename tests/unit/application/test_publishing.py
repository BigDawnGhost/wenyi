"""Publishing service contracts independent of legacy storage and rendering."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from trans_novel.application.publishing import PublishingOptions, assemble_outputs


def _options(**overrides: Any) -> PublishingOptions:
    values = {
        "mono": True,
        "bilingual": False,
        "bilingual_order": "target_first",
        "bilingual_preserve_source_style": False,
        "about_page": True,
    }
    values.update(overrides)
    return PublishingOptions(**values)


def test_mono_and_bilingual_outputs_preserve_order_and_renderer_arguments() -> None:
    store = object()
    progress_updates: list[tuple[int, int, str]] = []
    rendered: list[tuple[tuple[object, ...], dict[str, object]]] = []
    stages: list[str] = []

    def renderer(*args: object, **kwargs: object) -> str:
        rendered.append((args, kwargs))
        return str(kwargs["out_path"])

    def stage_call(name: str, operation, *args: object, **kwargs: object) -> str:
        stages.append(name)
        return operation(*args, **kwargs)

    outputs = assemble_outputs(
        store,
        input_path="source.epub",
        progress=lambda done, total, label: progress_updates.append((done, total, label)),
        out_format="epub",
        out_path="translated.epub",
        pdf_engine="fpdf2",
        options=_options(
            bilingual=True,
            bilingual_order="source_first",
            bilingual_preserve_source_style=True,
            about_page=False,
        ),
        renderer=renderer,
        bilingual_path=lambda path: f"{path}.bilingual",
        stage_call=stage_call,
    )

    assert outputs == ["translated.epub", "translated.epub.bilingual"]
    assert progress_updates == [(0, 0, "回填译文…")]
    assert stages == ["assemble", "assemble"]
    assert rendered == [
        (
            (store, "source.epub"),
            {
                "out_path": "translated.epub",
                "out_format": "epub",
                "bilingual": False,
                "about_page": False,
                "pdf_engine": "fpdf2",
            },
        ),
        (
            (store, "source.epub"),
            {
                "out_path": "translated.epub.bilingual",
                "out_format": "epub",
                "bilingual": True,
                "order": "source_first",
                "preserve_source_style": True,
                "about_page": False,
                "pdf_engine": "fpdf2",
            },
        ),
    ]


def test_disabling_both_variants_keeps_historical_mono_fallback() -> None:
    calls: list[dict[str, object]] = []

    def renderer(*_args: object, **kwargs: object) -> str:
        calls.append(kwargs)
        return "fallback.txt"

    outputs = assemble_outputs(
        object(),
        input_path="source.txt",
        progress=None,
        out_format="txt",
        out_path=None,
        pdf_engine="weasyprint",
        options=_options(mono=False, bilingual=False),
        renderer=renderer,
        bilingual_path=lambda _path: "must-not-be-called",
        stage_call=lambda _name, operation, *args, **kwargs: operation(*args, **kwargs),
    )

    assert outputs == ["fallback.txt"]
    assert len(calls) == 1
    assert calls[0]["bilingual"] is False


def test_default_bilingual_path_is_left_to_renderer_when_out_path_is_absent() -> None:
    received_out_paths: list[object] = []

    def renderer(*_args: object, **kwargs: object) -> str:
        received_out_paths.append(kwargs["out_path"])
        return "default-bilingual.epub"

    outputs = assemble_outputs(
        object(),
        input_path="source.epub",
        progress=None,
        out_format="epub",
        out_path=None,
        pdf_engine="weasyprint",
        options=_options(mono=False, bilingual=True),
        renderer=renderer,
        bilingual_path=lambda _path: (_ for _ in ()).throw(AssertionError("unexpected call")),
        stage_call=lambda _name, operation, *args, **kwargs: operation(*args, **kwargs),
    )

    assert outputs == ["default-bilingual.epub"]
    assert received_out_paths == [None]


def test_publishing_import_has_no_concrete_runtime_dependencies() -> None:
    script = """
import sys
import trans_novel.application.publishing

forbidden = (
    "trans_novel.assemble.writer",
    "trans_novel.cli",
    "trans_novel.config",
    "trans_novel.glossary.store",
    "trans_novel.pipeline.orchestrator",
    "trans_novel.pipeline.runstore",
    "langgraph",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected publishing dependencies: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
