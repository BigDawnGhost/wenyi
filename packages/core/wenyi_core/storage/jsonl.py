"""File-backed append-only JSON records, under the caller's existing writer lock."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from .io_path import to_io_path


def is_durable_record(record: dict) -> bool:
    """Whether to sync this record before returning to its writer."""
    event = record.get("event")
    return isinstance(event, str) and event == "batch_glossary_extracted"


def append_jsonl_record(path: str | Path, record: dict) -> None:
    """Append without letting an unterminated old line absorb the new record."""
    sync = is_durable_record(record)
    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    io_path = to_io_path(path)
    try:
        handle = open(io_path, "a+b")
    except PermissionError:
        # Preserve append access to a write-only log. Readers ignore an extra blank line.
        handle = open(io_path, "ab")
    with handle:
        needs_separator = not handle.readable()
        if not needs_separator and handle.seek(0, os.SEEK_END):
            handle.seek(-1, os.SEEK_END)
            needs_separator = handle.read(1) != b"\n"
        if needs_separator:
            encoded = b"\n" + encoded
        handle.write(encoded)
        if sync:
            handle.flush()
            os.fsync(handle.fileno())


def iter_jsonl_records(path: str | Path) -> Iterator[dict]:
    """Isolate malformed JSON or UTF-8 to its own byte-delimited line."""
    try:
        handle = open(to_io_path(path), "rb")
    except FileNotFoundError:
        return
    with handle:
        for line in handle:
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(row, dict):
                yield row
