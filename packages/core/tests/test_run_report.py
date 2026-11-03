"""The run report is read-only and must survive a partial event log."""

import json
from io import StringIO
from typing import Any

from rich.console import Console
from typer.testing import CliRunner
from wenyi_cli.cli import create_app
from wenyi_cli.commands.run_report import build_report
from wenyi_core.llm.usage import UsageSample, UsageTracker
from wenyi_core.storage.run_store import RunStore


class FakeStore:
    def __init__(self, tmp_path):
        self.run_dir = str(tmp_path)
        self.event_log_path = str(tmp_path / "events.jsonl")
        self._usage: dict[str, Any] | None = None

    def load_usage(self):
        return self._usage


def write_events(store, events):
    with open(store.event_log_path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def test_a_failed_run_reports_the_error_type_and_traceback(tmp_path):
    store = FakeStore(tmp_path)
    write_events(
        store,
        [
            {"event": "chapter_done", "index": 1},
            {"event": "llm_retry_wait", "provider": "openai"},
            {
                "event": "pipeline_error",
                "error_type": "ProviderRequestError",
                "error": "Model request failed",
                "run_id": "run-7",
                "traceback": "Traceback ...",
            },
        ],
    )
    report = build_report(store)
    assert report["events"] == 3
    assert report["failures"] == 1
    assert report["retries"] == 1
    assert report["last_failure"]["error_type"] == "ProviderRequestError"
    assert report["last_failure"]["run_id"] == "run-7"


def test_a_crashed_step_counts_as_a_failure(tmp_path):
    # CLI pipelines never emit pipeline_error; a failed run is a step that started
    # but has no matching run_steps_finished (audit Z-2/H-1).
    store = FakeStore(tmp_path)
    write_events(
        store,
        [
            {
                "event": "run_steps_started",
                "steps": ["prepare", "translate"],
                "input_path": "b.txt",
            },
            {"event": "run_steps_finished", "steps": ["prepare"]},
        ],
    )
    report = build_report(store)
    assert report["failures"] == 1
    assert report["last_failure"]["error_type"] is None
    assert report["last_failure"]["error"] is None


def test_retry_exhaustion_surfaces_as_the_last_failure(tmp_path):
    store = FakeStore(tmp_path)
    write_events(
        store,
        [
            {"event": "llm_retry_wait", "provider": "openai"},
            {
                "event": "llm_retry_exhausted",
                "provider": "openai",
                "error_type": "ProviderRequestError",
                "status_code": 503,
            },
        ],
    )
    report = build_report(store)
    assert report["retries"] == 2  # wait + exhausted both start with llm_retry
    assert report["last_failure"]["error_type"] == "ProviderRequestError"


def test_a_torn_multibyte_final_line_keeps_the_earlier_records(tmp_path):
    # ensure_ascii=False records tear inside CJK characters when the process dies
    # mid-append; the report must survive instead of crashing on UnicodeDecodeError
    # (audit Z-2/H-2).
    store = FakeStore(tmp_path)
    record = json.dumps({"event": "chapter_done", "title": "第十二章"}, ensure_ascii=False)
    torn = "第五十章：暗流".encode("utf-8")[:6]
    with open(store.event_log_path, "wb") as handle:
        handle.write(record.encode("utf-8") + b"\n")
        handle.write(b'{"event": "chapter_done", "title": "\n')
        handle.write(torn)
    report = build_report(store)
    assert report["events"] == 1
    assert report["failures"] == 0


def test_a_torn_final_line_keeps_the_earlier_records(tmp_path):
    store = FakeStore(tmp_path)
    with open(store.event_log_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "chapter_done"}) + "\n")
        # An interrupted append leaves half a record behind.
        handle.write('{"event": "pipeline_er')
    report = build_report(store)
    assert report["events"] == 1
    assert report["failures"] == 0


def test_a_run_without_failures_says_so(tmp_path):
    store = FakeStore(tmp_path)
    write_events(store, [{"event": "chapter_done"}])
    report = build_report(store)
    assert report["last_failure"] is None
    assert report["pauses"] == 0


def test_usage_totals_come_from_the_ledger(tmp_path):
    store = FakeStore(tmp_path)
    write_events(store, [])
    tracker = UsageTracker()
    tracker.record("cheap", UsageSample(120, 30, 150, cache_hit_tokens=40))
    store._usage = tracker.summary()
    report = build_report(store)
    assert report["usage"]["prompt_tokens"] == 120
    assert report["usage"]["completion_tokens"] == 30
    assert report["usage"]["cache_hit_tokens"] == 40


def test_inspect_run_displays_the_real_usage_schema(tmp_path, monkeypatch):
    store = RunStore(str(tmp_path / "run"))
    store.save_manifest({"chapters": []})
    tracker = UsageTracker()
    tracker.record("cheap", UsageSample(120, 30, 150, cache_hit_tokens=40))
    store.save_usage(tracker.summary())
    monkeypatch.setattr("wenyi_cli.commands.inspection.runstore_for_cli", lambda *a, **kw: store)
    output = StringIO()
    app = create_app(console=Console(file=output, width=120))
    result = CliRunner().invoke(
        app, ["-c", str(tmp_path / "config.yaml"), "inspect-run", "synthetic.txt"]
    )
    assert result.exit_code == 0, result.output
    assert "Tokens: in 120  out 30  cache read 40" in output.getvalue()


def test_a_missing_run_directory_reports_empty_rather_than_failing(tmp_path):
    store = FakeStore(tmp_path)
    report = build_report(store)
    assert report["events"] == 0
    assert report["failures"] == 0
    assert report["usage"] == {}
