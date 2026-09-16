"""Lightweight subtitle state without a glossary.
The selected state/srt run directory contains manifest.json for source identity/window
settings/progress, cues.jsonl for targets/status, batches/ for raw cached responses,
usage.json for cumulative tokens and events.jsonl for append-only actions/retries.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from ..i18n.languages import validate_run_languages
from ..pipeline.runstore import source_sha256, translation_run_dir
from ..storage.artifacts import FileArtifacts
from ..timing import save_timing

STATUS_PENDING = "pending"
STATUS_DONE = "done"


class SrtRunStore:
    """Subtitle run storage: manifest, cues, batch cache, usage and events."""

    def __init__(self, run_dir: str, *, storage=None):
        self.run_dir = run_dir
        self._usage_checkpoint: dict[str, Any] | None = None
        self.batches_dir = os.path.join(run_dir, "batches")
        self._storage = storage
        self._artifacts = storage or FileArtifacts(run_dir)
        if storage is None:
            os.makedirs(self.batches_dir, exist_ok=True)

    def _key(self, path: str) -> str:
        relative = os.path.relpath(path, self.run_dir).replace(os.sep, "/")
        return f"srt/{relative}" if self._storage is not None else relative

    def load_manifest(self) -> dict[str, Any]:
        return self._read_json(self.manifest_path)

    @classmethod
    def for_source(cls, state_dir: str, source_path: str, target_lang: str = "zh") -> "SrtRunStore":
        """Locate subtitle state by source filename slug."""
        stem = os.path.splitext(os.path.basename(source_path))[0]
        run_dir = translation_run_dir(os.path.join(state_dir, "srt"), stem, target_lang)
        return cls(run_dir)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def cues_path(self) -> str:
        return os.path.join(self.run_dir, "cues.jsonl")

    @property
    def usage_path(self) -> str:
        return os.path.join(self.run_dir, "usage.json")

    @property
    def event_log_path(self) -> str:
        return os.path.join(self.run_dir, "events.jsonl")

    @contextmanager
    def _file_lock(self, filename: str) -> Iterator[None]:
        """Serialize cross-process operations using the named lock file within state."""
        if self._storage is not None:
            with self._storage.state_lock():
                yield
            return
        os.makedirs(self.run_dir, exist_ok=True)
        lock_path = os.path.join(self.run_dir, filename)
        with open(lock_path, "a+b") as lock_file:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def event_lock(self) -> Iterator[None]:
        """Serialize JSONL appends so concurrent writers cannot interleave one line."""
        with self._file_lock(".events.lock"):
            yield

    def ensure_manifest(
        self,
        source_path: str,
        *,
        cue_count: int,
        title: str | None = None,
        source_lang: str = "auto",
        target_lang: str = "zh",
        batch_size: int = 20,
        overlap_size: int = 10,
        max_concurrent: int = 100,
    ) -> dict[str, Any]:
        """Initialize or validate source identity, then return the manifest."""
        digest = source_sha256(source_path)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        manifest = self.load_manifest()
        if manifest:
            validate_run_languages(manifest, source_lang, target_lang)
            if manifest.get("source_sha256") != digest:
                raise ValueError(
                    "Subtitle source does not match existing state; use a separate state directory."
                )
            return manifest
        stem = os.path.splitext(os.path.basename(source_path))[0]
        manifest = {
            "fmt": "srt",
            "title": title or stem,
            "source_path": os.path.abspath(source_path),
            "source_sha256": digest,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "cue_count": cue_count,
            "done_count": 0,
            "status": "running",
            "batch_size": batch_size,
            "overlap_size": overlap_size,
            "max_concurrent": max_concurrent,
            "created_at": now,
            "updated_at": now,
        }
        self._write_json(self.manifest_path, manifest)
        return manifest

    def update_manifest(self, **fields: Any) -> dict[str, Any]:
        """Merge manifest fields and refresh updated_at."""
        manifest = self.load_manifest()
        manifest.update(fields)
        manifest["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._write_json(self.manifest_path, manifest)
        return manifest

    def load_cues(self) -> dict[str, dict[str, Any]]:
        """Read cues.jsonl indexed by cue number, or return an empty mapping if absent."""
        if self._storage is not None:
            data = self._artifacts.read_artifact(self._key(self.cues_path))
            return data if isinstance(data, dict) else {}
        rows = self._artifacts.read_artifact_records(self._key(self.cues_path))
        return {str(row["index"]): row for row in rows if row.get("index") is not None}

    def save_cues(self, cues: dict[str, dict[str, Any]]) -> None:
        """Save cue state atomically in the selected persistence backend."""
        if self._storage is not None:
            self._artifacts.write_artifact(self._key(self.cues_path), cues)
            return

        def sort_key(index: str):
            try:
                return (0, int(index), index)
            except ValueError:
                return (1, 0, index)

        os.makedirs(self.run_dir, exist_ok=True)
        tmp = f"{self.cues_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for index in sorted(cues, key=sort_key):
                handle.write(json.dumps(cues[index], ensure_ascii=False) + "\n")
        os.replace(tmp, self.cues_path)

    def ensure_cues(
        self,
        source_cues: list[tuple[str, str, str]],
    ) -> dict[str, dict[str, Any]]:
        """Initialize or fill cues from source while preserving existing target/status.
        Each source_cues item contains index, timestamp and source text.
        """
        existing = self.load_cues()
        merged: dict[str, dict[str, Any]] = {}
        for index, timestamp, source in source_cues:
            key = str(index)
            prev = existing.get(key)
            if prev is not None:
                row = dict(prev)
                row["timestamp"] = timestamp
                row["source"] = source
                if not row.get("target"):
                    row["status"] = STATUS_PENDING
                merged[key] = row
            else:
                merged[key] = {
                    "index": key,
                    "timestamp": timestamp,
                    "source": source,
                    "target": "",
                    "status": STATUS_PENDING,
                }
        self.save_cues(merged)
        return merged

    def translations_from_cues(self, cues: dict[str, dict[str, Any]]) -> dict[str, str]:
        """Extract completed translations from the cue mapping."""
        out: dict[str, str] = {}
        for index, row in cues.items():
            target = row.get("target")
            status = row.get("status")
            if isinstance(target, str) and target and status == STATUS_DONE:
                out[str(index)] = target
            elif isinstance(target, str) and target:
                out[str(index)] = target
        return out

    def apply_translations(
        self,
        cues: dict[str, dict[str, Any]],
        translations: dict[str, str],
        *,
        status: str = STATUS_DONE,
    ) -> dict[str, dict[str, Any]]:
        """Update translations in the in-memory cue mapping without persisting."""
        for index, target in translations.items():
            key = str(index)
            row = cues.get(key)
            if row is None:
                continue
            row["target"] = target
            row["status"] = status
        return cues

    def batch_path(self, batch_start: int) -> str:
        return os.path.join(self.batches_dir, f"{batch_start:06d}.json")

    def load_batch(self, batch_start: int) -> dict[str, str] | None:
        path = self.batch_path(batch_start)
        data = self._read_json(path)
        raw = data.get("translations")
        if not isinstance(raw, dict):
            return None
        return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}

    def save_batch(self, batch_start: int, translations: dict[str, str]) -> None:
        self._write_json(
            self.batch_path(batch_start),
            {"batch_start": batch_start, "translations": translations},
        )

    def save_usage(self, data: dict[str, Any]) -> None:
        """Atomically save cumulative token usage."""
        if self._storage is not None:
            self._storage.save_usage(data)
        else:
            self._write_json(self.usage_path, data)

    def record_timing(self, record: dict[str, Any]) -> dict[str, Any]:
        """Accumulate subtitle invocation timing independently of token usage."""
        if self._storage is not None:
            return self._storage.record_timing(record)
        with self._file_lock(".timing.lock"):
            return save_timing(self.run_dir, record)

    def load_usage(self) -> dict[str, Any] | None:
        """Read cumulative token usage, or return None if absent."""
        if self._storage is not None:
            return self._storage.load_usage()
        return self._artifacts.read_artifact(self._key(self.usage_path))

    def log_event(self, event: str, **data: Any) -> None:
        """Append one JSONL event."""
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        if self._storage is not None:
            self._storage.log_event(event, **data)
        else:
            with self.event_lock():
                self._artifacts.append_artifact_record(self._key(self.event_log_path), row)

    def _read_json(self, path: str) -> dict[str, Any]:
        data = self._artifacts.read_artifact(self._key(path))
        return data if isinstance(data, dict) else {}

    def _write_json(self, path: str, data: dict[str, Any]) -> None:
        self._artifacts.write_artifact(self._key(path), data)
        if self._storage is not None and path == self.manifest_path:
            self._storage.save_manifest({**data, "initialized": True, "chapters": []})
