"""Aggregate one run's timing, usage and failure events into a single report."""

from __future__ import annotations

import json
import os
from typing import Any

from wenyi_core.timing import load_timing


def _read_events(path: str) -> list[dict[str, Any]]:
    """Read the append-only log, skipping a torn final line.

    Records are appended with ``ensure_ascii=False``, so an interrupted write can tear a
    multi-byte character in half; strict text-mode decoding would then raise before any
    record is read (audit Z-2/H-2). Read bytes and drop undecodable lines instead.
    """
    if not os.path.isfile(path):
        return []
    events: list[dict[str, Any]] = []
    with open(path, "rb") as handle:
        raw = handle.read()
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            # Torn multi-byte append: the earlier records are still valid.
            continue
        try:
            events.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return events


def _usage_totals(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Return the ledger totals; the ledger already sums them by tier."""
    if not usage or not isinstance(usage, dict):
        return {}
    totals = usage.get("totals")
    return dict(totals) if isinstance(totals, dict) else {}


def _crashed_step_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``run_steps_started`` events whose steps never finished.

    CLI/Core pipelines never emit the backend runner's ``pipeline_error`` events; on the
    CLI a failed run is a step that started but has no matching ``run_steps_finished``
    (audit Z-2/H-1). Resume invocations restart the step list, so finished steps from
    any invocation count as done.
    """
    finished_steps: set[str] = set()
    started_events: list[dict[str, Any]] = []
    for event in events:
        name = event.get("event")
        steps = event.get("steps")
        if not isinstance(steps, list):
            continue
        if name == "run_steps_started":
            started_events.append(event)
        elif name == "run_steps_finished":
            finished_steps.update(step for step in steps if isinstance(step, str))
    return [
        event
        for event in started_events
        if not {step for step in event.get("steps") or [] if isinstance(step, str)}
        <= finished_steps
    ]


def build_report(store) -> dict[str, Any]:
    """Collect what a failed or slow run would need to explain itself."""
    events = _read_events(store.event_log_path)
    failures = [event for event in events if event.get("event") == "pipeline_error"]
    failures.extend(_crashed_step_events(events))
    retries = [event for event in events if str(event.get("event", "")).startswith("llm_retry")]
    exhausted = [event for event in events if event.get("event") == "llm_retry_exhausted"]
    paused = [event for event in events if event.get("event") == "task_paused"]
    timing = load_timing(store.run_dir)
    usage = store.load_usage()

    last_failure = exhausted[-1] if exhausted else (failures[-1] if failures else None)
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
