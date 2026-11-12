"""File implementation of the backend-neutral JSON artifact port.

Keys are relative to a run; domain services never open state paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Any

from ..errors import ArtifactCorrupt
from .atomic_write import write_text_atomic
from .io_path import to_io_path


class FileArtifacts:
    def __init__(self, run_dir: str):
        self._artifact_root = os.path.abspath(run_dir)
        self._artifact_lock = RLock()

    def _artifact_path(self, key: str) -> Path:
        # Resolve both paths in the same Windows namespace, even for a short root.
        root = Path(self._io_path(self._artifact_root)).resolve()
        path = Path(self._io_path(Path(self._artifact_root, key))).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Artifact key must remain within the run")
        return path

    @staticmethod
    def _io_path(path: str | Path) -> str:
        """Use absolute extended-length drive/UNC paths on Windows; leave POSIX unchanged."""
        return to_io_path(path)

    def read_artifact(self, key: str) -> Any | None:
        try:
            raw = self._artifact_path(key).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ArtifactCorrupt(f"Cannot read artifact {key}") from error
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ArtifactCorrupt(f"Artifact {key} is corrupt at byte {error.pos}") from error

    def write_artifact(self, key: str, value: Any) -> None:
        path = self._artifact_path(key)
        with self._artifact_lock:
            write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2))

    def delete_artifact(self, key: str) -> None:
        self._artifact_path(key).unlink(missing_ok=True)

    def list_artifacts(self, prefix: str = "") -> list[str]:
        base = self._artifact_path("")
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
        with self._artifact_lock, open(to_io_path(path), "a", encoding="utf-8") as handle:
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
