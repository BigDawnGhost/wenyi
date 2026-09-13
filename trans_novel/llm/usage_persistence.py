"""Crash-safe synchronous persistence for schema-v2 usage snapshots."""

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from trans_novel.llm.usage import UsageTracker, merge_usage_summaries, usage_delta


class UsagePersistenceError(RuntimeError):
    """A local usage durability failure; never a provider retry/fallback error."""


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WAL_SCHEMA_VERSION = 1


class UsagePersistence:
    """Owns the usage.json/WAL pair and synchronously commits tracker snapshots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usage_path: str | None = None
        self._tracker: UsageTracker | None = None
        self._cumulative: dict[str, Any] = {}
        self._tracker_checkpoint: dict[str, Any] = {}
        self._scope_checkpoint: dict[str, Any] = {}
        self._scope: str | None = None
        self._recovered_tracker_after: dict[str, Any] | None = None
        self._event_callback: Callable[[str, dict[str, Any]], None] | None = None

    @property
    def wal_path(self) -> str:
        if self._usage_path is None:
            raise UsagePersistenceError("usage persistence is not bound")
        return os.path.join(os.path.dirname(self._usage_path), "usage.wal.json")

    def bind(
        self,
        usage_path: str,
        tracker: UsageTracker,
        *,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if tracker.has_active_work():
            raise UsagePersistenceError(
                "cannot rebind usage while a scope or provider attempt is active"
            )
        checkpoint = tracker.summary()
        tracker.bind_persistence(self)
        with self._lock:
            same_binding = self._usage_path == usage_path and self._tracker is tracker
            self._usage_path = usage_path
            self._event_callback = event_callback
            self._recover_locked()
            if same_binding:
                return
            loaded = self._load_usage_locked()
            self._cumulative = loaded or {}
            self._tracker = tracker
            self._tracker_checkpoint = checkpoint
            self._scope_checkpoint = {}
            self._scope = None
            self._recovered_tracker_after = None

    def begin_scope(self, scope: str | None) -> None:
        if scope is None:
            return
        tracker = self._tracker
        if tracker is None:
            raise UsagePersistenceError("usage persistence is not bound")
        tracker.begin_scope()
        with self._lock:
            if self._scope is not None:
                tracker.end_scope()
                raise UsagePersistenceError("usage scope is already active")
        checkpoint = tracker.summary()
        with self._lock:
            self._scope = scope
            self._scope_checkpoint = checkpoint

    def finish_scope(self, scope: str | None) -> None:
        if scope is None:
            return
        tracker = self._tracker
        if tracker is None:
            raise UsagePersistenceError("usage persistence is not bound")
        with self._lock:
            if self._scope != scope:
                raise UsagePersistenceError("usage scope does not match its binding")
            checkpoint = self._scope_checkpoint
        current = tracker.summary()
        increment = usage_delta(current, checkpoint)
        tracker.end_scope()
        with self._lock:
            if self._scope != scope:
                raise UsagePersistenceError("usage scope changed while finishing")
            self._scope = None
            self._scope_checkpoint = current
            if self._event_callback is not None and _has_activity(increment):
                self._event_callback(scope, increment)

    def persist(self, snapshot: dict[str, Any]) -> None:
        """Commit one already-accounted tracker snapshot before its caller returns."""
        with self._lock:
            self._recover_locked()
            if self._recovered_tracker_after is not None:
                if snapshot == self._recovered_tracker_after:
                    self._tracker_checkpoint = snapshot
                    self._recovered_tracker_after = None
                    return
                self._tracker_checkpoint = self._recovered_tracker_after
                self._recovered_tracker_after = None
            increment = usage_delta(snapshot, self._tracker_checkpoint)
            if not _has_activity(increment):
                return
            cumulative = merge_usage_summaries(self._cumulative, increment)
            try:
                before = self._read_usage_bytes_locked()
                before_hash = _sha256(before) if before is not None else None
                after = _serialize(cumulative)
                after_hash = _sha256(after)
                wal = {
                    "wal_schema_version": _WAL_SCHEMA_VERSION,
                    "before_sha256": before_hash,
                    "after_sha256": after_hash,
                    "after": cumulative,
                    "tracker_after": snapshot,
                }
                self._atomic_write(self.wal_path, _serialize(wal))
                self._replace_usage(after)
                self._clear_wal()
            except UsagePersistenceError:
                raise
            except Exception as error:
                raise UsagePersistenceError(f"unable to persist usage: {error}") from error
            self._cumulative = cumulative
            self._tracker_checkpoint = snapshot

    def _load_usage_locked(self) -> dict[str, Any]:
        raw = self._read_usage_bytes_locked()
        if raw is None:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("usage snapshot must be an object")
            merge_usage_summaries({}, value)
            return value
        except Exception as error:
            raise UsagePersistenceError(f"invalid schema-v2 usage.json: {error}") from error

    def _read_usage_bytes_locked(self) -> bytes | None:
        if self._usage_path is None:
            raise UsagePersistenceError("usage persistence is not bound")
        try:
            with open(self._usage_path, "rb") as stream:
                return stream.read()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise UsagePersistenceError(f"unable to read usage.json: {error}") from error

    def _recover_locked(self) -> None:
        wal_path = self.wal_path
        if not os.path.isfile(wal_path):
            return
        try:
            with open(wal_path, encoding="utf-8") as stream:
                wal = json.load(stream)
            _validate_wal(wal)
            current = self._read_usage_bytes_locked()
            current_hash = _sha256(current) if current is not None else None
            before_hash = wal["before_sha256"]
            after_hash = wal["after_sha256"]
            if current_hash == before_hash:
                self._replace_usage(_serialize(wal["after"]))
                self._cumulative = wal["after"]
                self._recovered_tracker_after = wal["tracker_after"]
            elif current_hash == after_hash:
                self._cumulative = wal["after"]
                self._recovered_tracker_after = wal["tracker_after"]
            else:
                raise UsagePersistenceError("usage WAL does not match current usage.json")
            self._clear_wal()
        except UsagePersistenceError:
            raise
        except Exception as error:
            raise UsagePersistenceError(f"unable to recover usage WAL: {error}") from error

    def _replace_usage(self, data: bytes) -> None:
        if self._usage_path is None:
            raise UsagePersistenceError("usage persistence is not bound")
        self._atomic_write(self._usage_path, data)

    def _clear_wal(self) -> None:
        try:
            os.remove(self.wal_path)
        except FileNotFoundError:
            return
        except OSError as error:
            raise UsagePersistenceError(f"unable to clear usage WAL: {error}") from error
        _fsync_directory(os.path.dirname(self.wal_path))

    @staticmethod
    def _atomic_write(path: str, data: bytes) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        temporary = path + ".tmp"
        try:
            with open(temporary, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(directory)
        except Exception:
            with suppress(FileNotFoundError):
                os.remove(temporary)
            raise


def _serialize(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_directory(directory: str) -> None:
    # Windows CRT 不支持目录文件描述符；文件 fsync 仍在写入流程中执行。
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _has_activity(increment: dict[str, Any]) -> bool:
    return bool(
        increment["by_agent"]
        or increment["by_operation"]
        or increment["by_provider"]
        or increment["by_model"]
        or increment["by_stage"]
        or any(increment["totals"].values())
    )


def _validate_wal(wal: Any) -> None:
    if not isinstance(wal, dict) or wal.get("wal_schema_version") != _WAL_SCHEMA_VERSION:
        raise UsagePersistenceError("unsupported usage WAL schema")
    required = {"wal_schema_version", "before_sha256", "after_sha256", "after", "tracker_after"}
    if not required.issubset(wal):
        raise UsagePersistenceError("malformed usage WAL")
    before = wal["before_sha256"]
    if before is not None and (not isinstance(before, str) or _SHA256.fullmatch(before) is None):
        raise UsagePersistenceError("malformed usage WAL before hash")
    after_hash = wal["after_sha256"]
    if not isinstance(after_hash, str) or _SHA256.fullmatch(after_hash) is None:
        raise UsagePersistenceError("malformed usage WAL after hash")
    after = wal["after"]
    if not isinstance(after, dict):
        raise UsagePersistenceError("malformed usage WAL after snapshot")
    if after_hash != _sha256(_serialize(after)):
        raise UsagePersistenceError("malformed usage WAL after hash")
    try:
        merge_usage_summaries({}, after)
    except Exception as error:
        raise UsagePersistenceError(f"malformed usage WAL after snapshot: {error}") from error
    tracker_after = wal["tracker_after"]
    if not isinstance(tracker_after, dict):
        raise UsagePersistenceError("malformed usage WAL tracker snapshot")
    try:
        merge_usage_summaries({}, tracker_after)
    except Exception as error:
        raise UsagePersistenceError(f"malformed usage WAL tracker snapshot: {error}") from error
