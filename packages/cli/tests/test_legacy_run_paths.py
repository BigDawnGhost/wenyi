"""CLI state lookup across the introduction of shortened slugs."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_cli.commands.inspection import describe_last_failure
from wenyi_core.ingest.source_hash import source_sha256
from wenyi_core.llm.providers.fake import FakeProvider
from wenyi_core.storage.io_path import to_io_path
from wenyi_core.storage.run_store import translation_run_dir


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = Path("Long title " + "a" * 62 + ".txt")
    source.write_text("# Opening\n\nThe story begins.\n", encoding="utf-8")
    config = Path("test.yaml")
    config.write_text(
        "language: {source: en, target: zh}\n"
        "llm: {preset: fake}\n"
        "pipeline: {book_understanding: false}\n"
        "paths: {state_dir: state}\n",
        encoding="utf-8",
    )
    short = Path(translation_run_dir("state", source.stem, "zh"))
    legacy = Path("state", "Long_title_" + "a" * 62, "targets", "zh")
    assert short != legacy

    def invoke(command, *options):
        return CliRunner().invoke(
            app,
            ["--config", str(config), *command.split(), str(source), *options],
            env={"COLUMNS": "160"},
        )

    return source, short, legacy, invoke


def test_prepare_and_resume_legacy_translation(book, monkeypatch):
    source, short, legacy, invoke = book
    default_request = FakeProvider._request

    def request(self, messages, model, *, json_mode, context):
        if context.operation == "translation.body":
            return '["开篇", "故事开始了。"]'
        if context.operation == "translation.title":
            return '["开篇"]'
        return default_request(self, messages, model, json_mode=json_mode, context=context)

    receipt = Path("translation.json")
    options = ("--chapter", "0", "--no-polish", "--result-json", str(receipt))
    with monkeypatch.context() as patch:
        patch.setattr(FakeProvider, "_request", request)
        initial = invoke("translate", *options)
    assert initial.exit_code == 0, initial.output
    assert Path(json.loads(receipt.read_text())["state_dir"]) == short.absolute()
    chapter = short / "chapters" / "ch0.json"
    original = chapter.read_bytes()
    assert any(segment["target"] for segment in json.loads(original)["segments"])
    short.parents[1].rename(legacy.parents[1])

    for command, flags, expected in (
        ("status", (), "done"),
        ("inspect-run", (), "Run:"),
        ("parse", (), legacy.parts[1]),
        ("prepare", (), legacy.parts[1]),
        ("translate", options, "Translated chapter 0"),
        ("assemble", ("--format", "txt", "--out", "result.txt"), "result.txt"),
        ("glossary list", (), "Translation"),
    ):
        result = invoke(command, *flags)
        assert result.exit_code == 0, (command, result.output, result.exception)
        assert expected in result.output
        assert not short.exists(), command
    result = json.loads(receipt.read_text())
    assert Path(result["state_dir"]) == legacy.absolute()
    assert result["source_sha256"] == source_sha256(source)
    assert (legacy / "chapters" / "ch0.json").read_bytes() == original
    assert "故事开始了。" in Path("result.txt").read_text(encoding="utf-8")


def test_prepare_resumes_legacy_initialization_marker(book):
    source, short, legacy, invoke = book
    legacy.mkdir(parents=True)
    marker = legacy / ".initializing.json"
    marker.write_text(json.dumps({"source_sha256": source_sha256(source)}), encoding="utf-8")
    events = legacy / "events.jsonl"
    events.write_text('{"event":"run_steps_started","steps":["prepare"]}\n', encoding="utf-8")
    assert "never finished: prepare" in (describe_last_failure(str(source), "test.yaml") or "")

    result = invoke("prepare")

    assert result.exit_code == 0, result.output
    assert (legacy / "manifest.json").is_file()
    assert not marker.exists()
    assert events.read_text(encoding="utf-8").startswith('{"event":"run_steps_started"')
    assert not short.exists()


def test_prepare_reuses_legacy_parse_only_state(book, monkeypatch):
    _, short, legacy, invoke = book
    result = invoke("parse")
    assert result.exit_code == 0, result.output
    short.parents[1].rename(legacy.parents[1])

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("compatible legacy source cache was not reused")

    monkeypatch.setattr(
        "wenyi_core.pipeline.prepare.input_preparation.load_document", unexpected_parse
    )
    result = invoke("prepare")
    assert result.exit_code == 0, result.output
    assert (legacy / "manifest.json").is_file()
    assert not short.exists()


def test_legacy_state_for_another_target_does_not_block_a_new_run(book):
    _, short, legacy, invoke = book
    other_target = legacy.with_name("fr")
    other_target.mkdir(parents=True)
    sentinel = other_target / "events.jsonl"
    sentinel.write_bytes(b"another target")
    result = invoke("prepare")
    assert result.exit_code == 0, result.output
    assert (short / "manifest.json").is_file()
    assert not legacy.exists()
    assert sentinel.read_bytes() == b"another target"


def test_inspect_legacy_events_beyond_windows_max_path(book):
    source, _, legacy, invoke = book
    deep_state = Path("state", "a" * 105, "b" * 105)
    config = Path("test.yaml")
    config.write_text(
        config.read_text().replace("state_dir: state", f"state_dir: {deep_state.as_posix()}"),
        encoding="utf-8",
    )
    run_dir = deep_state.joinpath(*legacy.parts[1:])
    assert len(str(run_dir.absolute())) > 260
    io_run_dir = Path(to_io_path(run_dir))
    io_run_dir.mkdir(parents=True)
    manifest = {"source_sha256": source_sha256(source), "source_lang": "en", "target_lang": "zh"}
    (io_run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (io_run_dir / "events.jsonl").write_text(
        '{"event":"pipeline_error","error_type":"TestError","error":"legacy diagnostic"}\n',
        encoding="utf-8",
    )

    result = invoke("inspect-run")

    assert result.exit_code == 0, result.output
    assert "legacy diagnostic" in result.output


@pytest.mark.parametrize(
    "problem", ["source", "target", "marker", "broken_marker", "unidentified", "both"]
)
def test_unsafe_legacy_state_is_not_reused_or_shadowed(book, problem):
    source, short, legacy, invoke = book
    legacy.mkdir(parents=True)
    digest = source_sha256(source)
    manifest = {
        "source_sha256": "0" * 64 if problem == "source" else digest,
        "source_lang": "en",
        "target_lang": "fr" if problem == "target" else "zh",
    }
    if problem == "marker":
        (legacy / ".initializing.json").write_text(
            json.dumps({"source_sha256": "0" * 64}), encoding="utf-8"
        )
    elif problem == "broken_marker":
        (legacy / ".initializing.json").write_text("[]", encoding="utf-8")
    elif problem != "unidentified":
        (legacy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if problem == "both":
        short.mkdir(parents=True)
        (short / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def files():
        return {path: path.read_bytes() for path in Path("state").rglob("*") if path.is_file()}

    before = files()

    expected = {
        "source": "does not match",
        "target": "Saved target language",
        "marker": "does not match",
        "broken_marker": "Cannot read saved translation state identity",
        "unidentified": "source identity",
        "both": "Both",
    }[problem]
    for command in ("prepare", "inspect-run"):
        result = invoke(command)
        assert result.exit_code == 1, result.output
        assert expected in result.output
    assert short.exists() == (problem == "both")
    assert files() == before
