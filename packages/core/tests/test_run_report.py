"""The run report is read-only and must survive a partial event log."""

import json

from wenyi_cli.commands.run_report import build_report


class FakeStore:
    def __init__(self, tmp_path):
        self.run_dir = str(tmp_path)
        self.event_log_path = str(tmp_path / "events.jsonl")
        self._usage = None

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
    store._usage = {"totals": {"input_tokens": 120, "output_tokens": 30}}
    report = build_report(store)
    assert report["usage"]["input_tokens"] == 120
    assert report["usage"]["output_tokens"] == 30


def test_a_missing_run_directory_reports_empty_rather_than_failing(tmp_path):
    store = FakeStore(tmp_path)
    report = build_report(store)
    assert report["events"] == 0
    assert report["failures"] == 0
    assert report["usage"] == {}
