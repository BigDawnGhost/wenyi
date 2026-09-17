"""File implementation of the backend-neutral JSON artifact port.

Keys are relative to a run; Review and subtitle services never open state paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any


class FileArtifacts:
    def __init__(self, run_dir: str):
        self._artifact_root = os.path.abspath(run_dir)
        self._artifact_lock = RLock()

    def _artifact_path(self, key: str) -> Path:
        path = Path(self._artifact_root, key).resolve()
        if not path.is_relative_to(Path(self._artifact_root).resolve()):
            raise ValueError("Artifact key must remain within the run")
        return path

    def read_artifact(self, key: str) -> Any | None:
        try:
            return json.loads(self._artifact_path(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_artifact(self, key: str, value: Any) -> None:
        path = self._artifact_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._artifact_lock:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)

    def delete_artifact(self, key: str) -> None:
        self._artifact_path(key).unlink(missing_ok=True)

    def list_artifacts(self, prefix: str = "") -> list[str]:
        base = Path(self._artifact_root).resolve()
        directory = self._artifact_path(prefix.rpartition("/")[0])
        if not directory.is_dir():
            return []
        keys = (
            path.relative_to(base).as_posix() for path in directory.rglob("*") if path.is_file()
        )
        return sorted(key for key in keys if key.startswith(prefix))

    def append_artifact_record(self, key: str, record: dict) -> None:
        path = self._artifact_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._artifact_lock, path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def read_artifact_records(self, key: str) -> list[dict]:
        try:
            lines = self._artifact_path(key).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows
