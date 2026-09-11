"""Invocation timing, resume accounting and stage-independent CLI clocks."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fake_llm import routing_handler
from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore
from trans_novel.srt.store import SrtRunStore
from trans_novel.srt.translate import translate_srt
from trans_novel.timing import RunTimer, format_duration, load_timing


@pytest.mark.parametrize("store_type", [RunStore, SrtRunStore])
def test_timing_accumulates_resumes_and_upserts_once(tmp_path, store_type):
    store = store_type(str(tmp_path / "run"))
    now = 10.0
    with RunTimer("translate", clock=lambda: now) as timer:
        store_before_binding = load_timing(store.run_dir)
        assert store_before_binding is None
        timer.store = store
        now = 75.5
    now = 10000.0
    assert timer.elapsed == 65.5
    with RunTimer("review", clock=lambda: now) as resumed:
        resumed.store = store
        now = 10035.0
    ledger = load_timing(store.run_dir)
    assert ledger is not None
    assert ledger["total_seconds"] == 100.5
    assert [run["operation"] for run in ledger["runs"]] == ["translate", "review"]
    store.record_timing(ledger["runs"][-1])
    assert load_timing(store.run_dir) == ledger


@pytest.mark.parametrize("error", [RuntimeError("failed"), KeyboardInterrupt()])
def test_interruption_saves_elapsed_and_preserves_exception(tmp_path, error):
    store = RunStore(str(tmp_path / "run"))
    now = 10.0
    with pytest.raises(type(error)) as caught:
        with RunTimer("translate", clock=lambda: now) as timer:
            timer.store = store
            now = 85.0
            raise error
    assert caught.value is error
    ledger = load_timing(store.run_dir)
    assert ledger is not None
    assert ledger["total_seconds"] == 75.0
    assert ledger["runs"][0]["status"] == (
        "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
    )


def test_timing_write_failure_preserves_previous_ledger_and_original_error(tmp_path, monkeypatch):
    store = RunStore(str(tmp_path / "run"))
    with RunTimer("prepare") as timer:
        timer.store = store
    before = load_timing(store.run_dir)

    def fail_replace(*args):
        raise OSError("disk full")

    monkeypatch.setattr("trans_novel.timing.os.replace", fail_replace)
    with pytest.raises(RuntimeError, match="workflow failed"):
        with RunTimer("translate") as timer:
            timer.store = store
            raise RuntimeError("workflow failed")
    assert load_timing(store.run_dir) == before
    assert not list(Path(store.run_dir).glob(".timing-*.tmp"))


@pytest.mark.parametrize("store_type", [RunStore, SrtRunStore])
def test_concurrent_timing_writers_do_not_lose_invocations(tmp_path, store_type):
    def write_record(index):
        store = store_type(str(tmp_path / "run"))
        return store.record_timing({"id": str(index), "elapsed_seconds": 5.0})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_record, range(12)))
    ledger = load_timing(str(tmp_path / "run"))
    assert ledger is not None
    assert ledger["total_seconds"] == 60.0
    assert len(ledger["runs"]) == 12


@pytest.fixture
def timed_book(tmp_path, monkeypatch):
    source = tmp_path / "book.txt"
    source.write_text("# A chapter\n\nThe morning was quiet.\n", encoding="utf-8")
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"polish": False, "review": False, "book_understanding": False},
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    now = [0.0]
    monkeypatch.setattr(
        "trans_novel.pipeline.runtime.RunTimer",
        lambda operation: RunTimer(operation, clock=lambda: now[0]),
    )
    monkeypatch.setattr("trans_novel.cli._load_config", lambda: config)

    def progress(done, total, label):
        now[0] += 2.0

    orch = Orchestrator(config, FakeClient(handler=routing_handler))
    return orch, source, now, progress


def test_full_workflow_counts_nested_stages_once_and_includes_export(timed_book, monkeypatch):
    orch, source, now, progress = timed_book
    original_export = orch._assembly.assemble_live

    def export(*args, **kwargs):
        outputs = original_export(*args, **kwargs)
        now[0] += 30.0
        return outputs

    monkeypatch.setattr(orch._assembly, "assemble_live", export)
    result = orch.run_all(str(source), out_format="txt", progress=progress)
    run_dir = result["store"].run_dir
    first_elapsed = now[0]
    first_ledger = load_timing(run_dir)
    assert first_ledger is not None
    assert first_ledger["total_seconds"] == first_elapsed
    assert first_elapsed >= 30.0
    assert len(first_ledger["runs"]) == 1

    now[0] = 10000.0
    orch.run_all(str(source), out_format="txt", progress=progress)
    ledger = load_timing(run_dir)
    assert ledger is not None
    assert len(ledger["runs"]) == 2
    assert ledger["total_seconds"] == first_elapsed + now[0] - 10000.0
    inspection = CliRunner().invoke(app, ["status", str(source)])
    assert inspection.exit_code == 0, inspection.output
    assert f"cumulative {format_duration(ledger['total_seconds'])}" in inspection.output
    orch.run_report(str(source))
    assert load_timing(run_dir) == ledger


def test_failed_translation_and_resumed_command_accumulate(timed_book, monkeypatch):
    orch, source, now, progress = timed_book
    store = orch.prepare_for_translation(str(source), progress=progress)
    prior = now[0]
    original_translate = orch._translation.run

    def fail_translation(*args, **kwargs):
        now[0] += 25.0
        raise KeyboardInterrupt()

    monkeypatch.setattr(orch._translation, "run", fail_translation)
    now[0] = 100.0
    with pytest.raises(KeyboardInterrupt):
        orch.run(str(source))
    failed = load_timing(store.run_dir)
    assert failed is not None
    assert failed["total_seconds"] == prior + 25.0
    assert failed["runs"][-1]["status"] == "interrupted"
    monkeypatch.setattr(orch._translation, "run", original_translate)
    now[0] = 10000.0
    orch.run(str(source), progress=progress)
    resumed = load_timing(store.run_dir)
    assert resumed is not None
    assert resumed["total_seconds"] == prior + 25.0 + now[0] - 10000.0
    assert len(resumed["runs"]) == 3


def test_wrong_source_does_not_modify_timing(timed_book):
    orch, source, now, progress = timed_book
    store = orch.prepare(str(source), progress=progress)
    before = Path(store.run_dir, "timing.json").read_bytes()
    source.write_text("# A chapter\n\nDifferent source content.\n", encoding="utf-8")
    with pytest.raises(ValueError):
        orch.run(str(source))
    assert Path(store.run_dir, "timing.json").read_bytes() == before


def test_individual_review_and_export_record_separate_invocations(timed_book):
    orch, source, now, progress = timed_book
    store = orch.run(str(source), progress=progress)
    orch.run_review(str(source), progress=progress)
    orch.run_assemble(str(source), out_format="txt", progress=progress)
    ledger = load_timing(store.run_dir)
    assert ledger is not None
    assert [run["operation"] for run in ledger["runs"]] == ["translate", "review", "assemble"]
    assert ledger["total_seconds"] == now[0]


def test_cli_retains_timing_summary_after_translation(timed_book, monkeypatch):
    orch, source, now, progress = timed_book
    monkeypatch.setattr(
        "trans_novel.pipeline.runtime.build_client", lambda cfg: FakeClient(handler=routing_handler)
    )
    result = CliRunner().invoke(app, ["translate", str(source), "--format", "txt"])
    assert result.exit_code == 0, result.output
    assert "Time: last run" in result.output
    assert "cumulative" in result.output
    assert "across 1 run." in result.output


def test_subtitle_resume_accumulates_time_in_subtitle_state(tmp_path, monkeypatch):
    source = tmp_path / "book.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello world.\n", encoding="utf-8")
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "paths": {"state_dir": str(tmp_path / "state")}}
    )
    now = [0.0]
    monkeypatch.setattr(
        "trans_novel.srt.translate.RunTimer",
        lambda operation: RunTimer(operation, clock=lambda: now[0]),
    )

    def progress(*args):
        now[0] += 3.0

    result = translate_srt(
        str(source),
        config,
        client=FakeClient(handler=lambda *args: '{"1": "你好。"}'),
        progress=progress,
    )
    first_elapsed = now[0]
    now[0] = 10000.0
    translate_srt(str(source), config, client=FakeClient(), progress=progress)
    ledger = load_timing(result["run_dir"])
    assert ledger is not None
    assert ledger["total_seconds"] == first_elapsed + now[0] - 10000.0
    assert len(ledger["runs"]) == 2


def test_duration_does_not_wrap_after_one_day():
    assert format_duration(90061.9) == "25:01:01"
