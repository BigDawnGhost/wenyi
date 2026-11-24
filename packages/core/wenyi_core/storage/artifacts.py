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
from .artifact_keys import validate_artifact_key
from .atomic_write import write_text_atomic
from .io_path import to_io_path

#: Appended records whose loss would repeat paid or completed work at the next exit.
#: Every other event stays buffered: fsync costs about 0.62 ms per record on an NTFS
#: volume against 0.03 ms for a buffered append, so an unconditional flush would tax
#: each translated batch for diagnostics nobody replays. The checkpoint is recognised
#: from the record itself so add-only ports keep their signature.
_DURABLE_EVENTS = frozenset({"batch_glossary_extracted"})


def is_durable_record(record: dict) -> bool:
    """Whether losing this appended record can discard a completed workflow step."""
    return record.get("event") in _DURABLE_EVENTS


class FileArtifacts:
    def __init__(self, run_dir: str):
        self._artifact_root = os.path.abspath(run_dir)
        self._artifact_lock = RLock()

    _artifact_key = staticmethod(validate_artifact_key)

    def _artifact_path(self, key: str, *, prefix: bool = False) -> Path:
        if not isinstance(key, str) or "\0" in key:
            # Reject NUL before path resolution so it never reaches a filesystem call.
            raise ValueError("Artifact key must be a portable relative path")
        # Resolve both paths in the same Windows namespace, even for a short root.
        root = Path(self._io_path(self._artifact_root)).resolve()
        path = Path(self._io_path(Path(self._artifact_root, key))).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Artifact key must remain within the run")
        self._artifact_key(key, prefix=prefix)
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
        self._artifact_key(prefix, prefix=True)
        base = self._artifact_path("", prefix=True)
        directory = self._artifact_path(prefix.rpartition("/")[0], prefix=True)
        if not directory.is_dir():
            return []
        keys = (
            path.relative_to(base).as_posix() for path in directory.rglob("*") if path.is_file()
        )
        return sorted(key for key in keys if key.startswith(prefix))

    def append_artifact_record(self, key: str, record: dict) -> None:
        """Append one record, syncing only a record a resumed run would replay.

        The append order and the artifact lock are unchanged. A checkpoint record is
        flushed and fsynced before the lock is released, so a power loss cannot leave
        the line buffered while the work it records still looks unfinished. Other
        records stay buffered, which is what keeps batch translation affordable.
        """
        path = self._artifact_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._artifact_lock, open(to_io_path(path), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            if is_durable_record(record):
                handle.flush()
                os.fsync(handle.fileno())

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
