"""Best-effort localization pointers for unexpected CLI failures.

The diagnostic log keeps the full traceback; the console only shows a short
pointer: the invocation run ID plus the latest entry of the active run's
append-only event log, so a failure can be located without reading the
traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

from wenyi_core.observability import current_run_id

_EVENT_LOG_NAME = "events.jsonl"


def latest_event_log_path(data_dir: Path) -> Path | None:
    """Return the most recently written event log under the state directory.

    Run-scoped commands append events right up to a failure, so the active
    run's ledger is the freshest one. Read-only and best-effort: callers show
    the remaining pointers regardless of the outcome.
    """
    state_dir = data_dir / "state"
    if not state_dir.is_dir():
        return None
    pattern = f"*/targets/*/{_EVENT_LOG_NAME}"
    candidates = [path for path in state_dir.glob(pattern) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def last_event_entry(path: Path) -> tuple[int, str] | None:
    """Return the 1-based sequence and name of the last complete event row.

    Mirrors the report reader: a torn final line from an interrupted append is
    skipped instead of failing the pointer.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    sequence = 0
    last: tuple[int, str] | None = None
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        sequence += 1
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and isinstance(row.get("event"), str):
            last = (sequence, row["event"])
    return last


def unexpected_error_message(error: Exception, config_path: str, run_id: str | None = None) -> str:
    """Build the concise console pointer shown when a command fails unexpectedly."""
    detail = str(error).strip()
    rendered = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
    lines = [f"Unexpected error: {rendered}"]
    invocation = run_id if run_id is not None else current_run_id()
    if invocation:
        lines.append(f"Run ID: {invocation}")
    data_dir = Path(config_path).resolve().parent
    event_log = latest_event_log_path(data_dir)
    if event_log is not None:
        entry = last_event_entry(event_log)
        if entry is not None:
            sequence, name = entry
            lines.append(f"Last event: #{sequence} {name} ({event_log})")
    lines.append("Full traceback: see the diagnostic log above.")
    return "\n".join(lines)
