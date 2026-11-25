"""Exercise explicit CLI output naming through real file-backed exports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import fake_pipeline_config
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator


@pytest.fixture
def book(tmp_path, monkeypatch):
    source = tmp_path / "book.txt"
    source.write_text("Original paragraph.", encoding="utf-8")
    cfg = fake_pipeline_config(str(tmp_path / "state"))
    cfg.pipeline.review = False
    fake = FakeClient(handler=routing_handler)
    store = Orchestrator(cfg, client=fake).run(str(source))
    store.close()
    monkeypatch.setattr("wenyi_cli.commands.context.CommandContext.load_config", lambda self: cfg)
    monkeypatch.setattr("wenyi_core.pipeline.run.runtime.build_client", lambda *a, **kw: fake)
    return source


def invoke(source, destination, *args, command="assemble"):
    return CliRunner().invoke(
        app, [command, str(source), "--format", "txt", "--out", str(destination), *args]
    )


@pytest.mark.parametrize("command", ["assemble", "translate"])
def test_late_destination_collision_preserves_other_writer(tmp_path, monkeypatch, book, command):
    from wenyi_core.assemble import text_writer

    destination = tmp_path / "edition.txt"
    render = text_writer._epub_native.write_text
    calls = []

    def other_writer_finishes_during_render(*args):
        calls.append(True)
        if not destination.exists():
            destination.write_bytes(b"external export")
        return render(*args)

    monkeypatch.setattr(text_writer._epub_native, "write_text", other_writer_finishes_during_render)
    receipt = tmp_path / "receipt.json"
    options = ["--result-json", str(receipt)] if command == "translate" else []
    result = invoke(book, destination, "--mono", "--bilingual", *options, command=command)
    assert result.exit_code == 0, result.output
    assert destination.read_bytes() == b"external export"
    assert len(calls) == 2  # No re-render for either edition.
    for name in ("edition (1).txt", "edition (1)-bi.txt"):
        assert (tmp_path / name).is_file() and name in result.output
    if command == "translate":
        outputs = json.loads(receipt.read_text(encoding="utf-8"))["outputs"]
        assert [Path(item["path"]) for item in outputs] == [
            (tmp_path / "edition (1).txt").resolve(),
            (tmp_path / "edition (1)-bi.txt").resolve(),
        ]


def test_collision_at_atomic_install_advances_to_next_number(book, tmp_path, monkeypatch):
    from wenyi_core import export_paths

    dest = tmp_path / "edition.txt"
    dest.write_bytes(b"existing")
    contested = tmp_path / "edition (1).txt"
    publish = export_paths._publish_new

    def finish_first(staged, target):
        if Path(target) == contested:
            contested.write_bytes(b"concurrent")
        publish(staged, target)

    monkeypatch.setattr(export_paths, "_publish_new", finish_first)
    result = invoke(book, dest, "--no-bilingual")
    assert result.exit_code == 0, result.output
    assert dest.read_bytes() == b"existing"
    assert contested.read_bytes() == b"concurrent"
    assert (tmp_path / "edition (2).txt").is_file()
    assert "edition (2).txt" in result.output


@pytest.mark.parametrize("mono", ["--mono", "--no-mono"])
def test_existing_bilingual_companion_moves_requested_editions(book, tmp_path, mono):
    dest = tmp_path / "edition.txt"
    companion = tmp_path / "edition-bi.txt"
    companion.write_bytes(b"existing")
    result = invoke(book, dest, mono, "--bilingual")
    assert result.exit_code == 0, result.output
    assert not dest.exists()
    assert companion.read_bytes() == b"existing"
    assert (tmp_path / "edition (1)-bi.txt").is_file()
    assert (tmp_path / "edition (1).txt").exists() == (mono == "--mono")


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_overwrite_or_free_name_keeps_requested_path(book, tmp_path, exists):
    dest = tmp_path / "edition.txt"
    if exists:
        dest.write_bytes(b"previous")
    result = invoke(book, dest, *(["--overwrite"] if exists else []))
    assert result.exit_code == 0, result.output
    assert dest.is_file() and dest.read_bytes() != b"previous"
    assert "Output exists" not in result.output


def test_source_destination_is_still_rejected(book):
    original = book.read_bytes()
    result = invoke(book, book, "--mono", "--no-bilingual")
    assert result.exit_code != 0
    assert "must not point at the source" in result.output
    assert book.read_bytes() == original


def test_failed_render_does_not_claim_output_or_leave_staging(book, tmp_path, monkeypatch):
    from wenyi_core.assemble import text_writer

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(text_writer._epub_native, "write_text", fail)
    result = invoke(book, tmp_path / "edition.txt", "--mono", "--no-bilingual")
    assert result.exit_code != 0
    assert not (tmp_path / "edition.txt").exists()
    assert not list(tmp_path.glob(".wenyi-export-*"))


def test_numbered_epub_keeps_warnings_with_the_actual_edition(book, tmp_path, monkeypatch):
    from wenyi_core.assemble import writer

    dest = tmp_path / "book.epub"
    previous = tmp_path / "book.epub.warnings.json"
    previous.write_text("other writer", encoding="utf-8")
    real_writer = writer._build_epub_from_chapters

    def warn(*args, warnings, **kwargs):
        warnings.append("Missing synthetic media")
        return real_writer(*args, warnings=warnings, **kwargs)

    monkeypatch.setattr(writer, "_build_epub_from_chapters", warn)
    result = CliRunner().invoke(
        app, ["assemble", str(book), "--format", "epub", "--out", str(dest)]
    )
    assert result.exit_code == 0, result.output
    assert not dest.exists()
    assert previous.read_text(encoding="utf-8") == "other writer"
    assert (tmp_path / "book (1).epub").is_file()
    assert json.loads((tmp_path / "book (1).epub.warnings.json").read_text(encoding="utf-8")) == [
        "Missing synthetic media"
    ]
