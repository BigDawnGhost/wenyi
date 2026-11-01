"""Explicit, local-only migrations of CLI file state; never run implicitly on load."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..llm.usage import convert_usage_ledger
from .file import FileStorage


def migrate_usage_ledgers(run_dir: str | Path) -> int:
    """Validate all ledgers, then back up and atomically convert each under the run lock.

    A failed conversion leaves its original bytes and backup intact. Successful earlier
    conversions are already current and skipped on retry. No Web/Desktop state is discovered.
    """
    root = Path(run_dir)
    store = FileStorage(str(root), create=False)
    if not store.exists():
        raise ValueError("Select a target run directory containing manifest.json")
    with store.lock():
        paths = [root / "usage.json", *sorted((root / "reviews").glob("review-*/usage.json"))]
        changes = []
        for path in paths:
            if path.is_file():
                original = path.read_bytes()
                value = json.loads(original)
                converted = convert_usage_ledger(value)
                if value != converted:
                    changes.append((path, original, converted))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        for path, original, converted in changes:
            with path.with_name(f"usage.before-routing-{stamp}.json").open("xb") as backup:
                backup.write(original)
            store.write_artifact(path.relative_to(root).as_posix(), converted)
    return len(changes)
