"""Aggregate one run's timing, usage and failure events into a single report."""

from __future__ import annotations

import json
import os
from typing import Any

from wenyi_core.timing import load_timing


def _read_events(path: str) -> list[dict[str, Any]]:
    """Read the append-only log, skipping a torn final line."""
    if not os.path.isfile(path):
        return []
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # An interrupted append can leave a partial line; earlier
                # records are still valid and are what the report is built from.
                continue
    return events


def _usage_totals(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Return the ledger totals; the ledger already sums them by tier."""
    if not usage or not isinstance(usage, dict):
        return {}
    totals = usage.get("totals")
    return dict(totals) if isinstance(totals, dict) else {}


def build_report(store) -> dict[str, Any]:
    """Collect what a failed or slow run would need to explain itself."""
    events = _read_events(store.event_log_path)
    failures = [event for event in events if event.get("event") == "pipeline_error"]
    retries = [event for event in events if str(event.get("event", "")).startswith("llm_retry")]
    paused = [event for event in events if event.get("event") == "task_paused"]
    timing = load_timing(store.run_dir)
    usage = store.load_usage()

    last_failure = failures[-1] if failures else None
    return {
        "run_dir": store.run_dir,
        "events": len(events),
        "failures": len(failures),
        "retries": len(retries),
        "pauses": len(paused),
        "last_failure": {
            "error_type": last_failure.get("error_type"),
            "error": last_failure.get("error"),
            "run_id": last_failure.get("run_id"),
            "traceback": last_failure.get("traceback"),
        }
        if last_failure
        else None,
        "timing": timing,
        "usage": _usage_totals(usage),
    }
