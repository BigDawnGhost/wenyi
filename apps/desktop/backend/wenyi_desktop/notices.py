"""Bounded status notifications for the native owner.

Notifications are a protocol channel of their own rather than diagnostic events:
a notice carries the project's display name, and the diagnostic boundary is
deliberately content-free. Both channels share the reporter's ordered writer, so
they cannot interleave; only this module decides what a notice may contain.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

KINDS = frozenset(
    {
        "task_failed",
        "task_timeout",
        "engine_stopped",
        "export_failed",
        "prepare_completed",
        "project_completed",
    }
)
TASKS = frozenset({"parse", "prepare", "translation", "chapter_translation", "review", "export"})
REASONS = frozenset(
    {"timeout", "permission", "io", "invalid", "dependency", "interrupted", "unknown"}
)
FAILURE_KINDS = frozenset({"task_failed", "task_timeout", "export_failed"})
MAX_PAYLOAD = 2048
MAX_NAME = 120
FIELDS = frozenset({"kind", "project_id", "project_name", "run_id", "task", "reason"})


def display_name(value: Any) -> str | None:
    """Keep a catalog display name printable and bounded, or reject it."""
    if not isinstance(value, str):
        return None
    # A control character separates words instead of joining them: "A\nBook"
    # must read as "A Book", not "ABook".
    cleaned = "".join(
        char if char.isprintable() and unicodedata.category(char) != "Cf" else " " for char in value
    )
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned[:MAX_NAME].rstrip()


def _identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and all(char.isascii() and (char.isalnum() or char in "-_.:") for char in value)
    )


def _task_payload(
    kind: str,
    project_id: Any,
    project_name: Any,
    run_id: Any,
    task: Any,
    reason: Any,
) -> dict | None:
    name = display_name(project_name)
    if not _identity(project_id) or not _identity(run_id) or task not in TASKS or name is None:
        return None
    payload: dict[str, Any] = {
        "kind": kind,
        "project_id": project_id,
        "project_name": name,
        "run_id": run_id,
        "task": task,
    }
    if kind in FAILURE_KINDS:
        if reason not in REASONS:
            return None
        payload["reason"] = reason
    elif reason is not None:
        return None
    return payload


def task_notice(
    kind: str,
    *,
    project: dict,
    run_id: str,
    task: str,
    reason: str | None = None,
) -> dict | None:
    """Build one task-scoped notice from catalog data, or None when invalid."""
    if kind not in KINDS - {"engine_stopped"} or not isinstance(project, dict):
        return None
    return _task_payload(kind, project.get("id"), project.get("name"), run_id, task, reason)


def encode(value: Any) -> str | None:
    """Validate one notice and render its protocol line, or None to drop it."""
    if not isinstance(value, dict) or not value.keys() <= FIELDS:
        return None
    kind = value.get("kind")
    if kind not in KINDS:
        return None
    if kind == "engine_stopped":
        if len(value) != 1:
            return None
        payload = {"kind": kind}
    else:
        payload = _task_payload(
            kind,
            value.get("project_id"),
            value.get("project_name"),
            value.get("run_id"),
            value.get("task"),
            value.get("reason"),
        )
        if payload is None:
            return None
    # The protocol stream is the engine's piped stdout, which Windows may encode
    # with the ANSI code page. Escapes keep the line ASCII, so a non-UTF-8 pipe
    # can neither corrupt the line nor raise while writing it.
    line = "WENYI_NOTICE " + json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(line.removeprefix("WENYI_NOTICE ").encode("utf-8")) > MAX_PAYLOAD:
        return None
    return line + "\n"
