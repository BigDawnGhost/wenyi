"""Reproducible experiment ledger for one pipeline run."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import Config
from ..llm.base import LLMClient
from ..llm.usage import usage_delta
from .runstore import source_sha256

if TYPE_CHECKING:
    from .runstore import RunStore


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "access_token",
    "api_token",
    "auth_token",
    "bearer_token",
)


def _package_version() -> str:
    """Read the installed package version with a stable fallback for an uninstalled source
    tree.
    """
    try:
        return version("trans-novel")
    except PackageNotFoundError:
        return "unknown"


def _now_iso() -> str:
    """Return sortable second-resolution time with the local timezone."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_value(value: Any, *, key: str = "") -> Any:
    """Convert configuration values to serializable data and redact possible credentials."""
    if _is_sensitive_key(key):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(item_key): _safe_value(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return f"<{type(value).__name__}>"


def _is_sensitive_key(key: str) -> bool:
    """Detect configuration keys that may contain credentials."""
    normalized = key.lower().replace("-", "_")
    return normalized == "token" or any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _fingerprint(data: Any) -> str:
    """Hash canonical JSON with SHA-256 for input/configuration comparisons."""
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_base_url(
    value: str | None,
    *,
    include_path: bool = False,
) -> str | None:
    """Preserve endpoint identity while removing possible URL credentials and query parameters."""
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        path = parsed.path if include_path else ""
        query = ""
        if include_path:
            safe_query = sorted(
                (key, item_value)
                for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
                if not _is_sensitive_key(key)
            )
            query = urlencode(safe_query)
        return urlunsplit((parsed.scheme, hostname, path, query, ""))
    except ValueError:
        # Never persist malformed URLs, including invalid ports, verbatim in the ledger.
        return "<invalid-url>"


def config_identity(config: Config) -> dict[str, Any]:
    """Extract nonsensitive configuration affecting translation and compute a stable
    fingerprint.
    """
    base_url = config.llm.base_url
    endpoint_identity = _safe_base_url(base_url, include_path=True)
    summary = {
        "language": {
            "source": config.source_lang,
            "target": config.target_lang,
        },
        "llm": {
            "provider": config.llm.provider,
            "base_url": _safe_base_url(base_url),
            "base_url_fingerprint": (
                _fingerprint(endpoint_identity) if endpoint_identity else None
            ),
            "reasoning_style": config.llm.reasoning_style,
            "timeout": config.llm.timeout,
            "max_retries": config.llm.max_retries,
            "tiers": {
                name: {
                    "model": tier.model,
                    "options": _safe_value(tier.options),
                }
                for name, tier in sorted(config.llm.tiers.items())
            },
        },
        "segment": _safe_value(config.segment.model_dump(mode="python")),
        "pipeline": _safe_value(config.pipeline.model_dump(mode="python")),
        "output": _safe_value(config.output.model_dump(mode="python")),
        "honorific_strategy": config.honorific_strategy,
    }
    return {"fingerprint": _fingerprint(summary), "summary": summary}


def input_identity(input_path: str) -> dict[str, Any]:
    """Record input filename, size and content fingerprint, excluding full paths and body text."""
    identity, _signature = _capture_input_identity(input_path)
    return identity


def _source_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return a cheap stable signature for detecting ordinary file replacement or edits."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _capture_input_identity(
    input_path: str,
) -> tuple[dict[str, Any], tuple[int, int, int, int, int] | None]:
    """Capture input identity and signature together; do not reuse a signature if hashing saw
    changes.
    """
    path = Path(input_path)
    identity: dict[str, Any] = {
        "name": path.name,
        "suffix": path.suffix.lower(),
        "exists": path.is_file(),
    }
    if not identity["exists"]:
        return identity, None

    before = _source_signature(path)
    try:
        if before is not None:
            identity["size_bytes"] = before[2]
        identity["sha256"] = source_sha256(str(path))
    except OSError:
        identity["readable"] = False
        return identity, None
    after = _source_signature(path)
    return identity, after if before == after else None


def _git_output(repo_root: Path, *args: str) -> str | None:
    """Read Git identity, falling back silently outside Git installations or on timeout."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def code_identity() -> dict[str, Any]:
    """Record package version and Git commit to trace experiments across branches."""
    repo_root = Path(__file__).resolve().parents[2]
    revision = _git_output(repo_root, "rev-parse", "HEAD")
    branch = _git_output(repo_root, "branch", "--show-current")
    status = _git_output(repo_root, "status", "--porcelain")
    return {
        "package_version": _package_version(),
        "git_revision": revision,
        "git_branch": branch,
        "git_dirty": bool(status) if revision else None,
    }


def _state_summary(store: RunStore) -> dict[str, int]:
    """Summarize final chapter/paragraph completion without copying book content."""
    manifest = store.load_manifest()
    chapters = manifest.get("chapters", [])
    summary = {
        "chapters_total": len(chapters),
        "chapters_translated": sum(item.get("status") == "done" for item in chapters),
        "segments_total": 0,
        "segments_translated": 0,
    }
    for item in chapters:
        chapter = store.load_chapter(item["index"])
        text_segments = chapter.text_segments
        summary["segments_total"] += len(text_segments)
        summary["segments_translated"] += sum(
            bool(segment.target and segment.target.strip()) for segment in text_segments
        )
    return summary


@dataclass
class RunMetricsRecorder:
    """Collect timing, usage and reproducible identity for one top-level operation."""

    operation: str
    requested_steps: list[str]
    input: dict[str, Any]
    config: dict[str, Any]
    code: dict[str, Any]
    usage_before: dict[str, Any]
    invocation: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(
        default_factory=lambda: (
            f"run-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f%z')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
    )
    started_at: str = field(default_factory=_now_iso)
    _started: float = field(default_factory=time.perf_counter)
    _store: RunStore | None = None
    _stage_seconds: dict[str, float] = field(default_factory=dict)
    _stage_started: dict[str, float] = field(default_factory=dict)
    _stage_depth: dict[str, int] = field(default_factory=dict)
    _finished: bool = False
    _input_signature: tuple[int, int, int, int, int] | None = field(
        default=None,
        repr=False,
    )
    _state_snapshot: dict[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def start(
        cls,
        *,
        operation: str,
        requested_steps: list[str],
        input_path: str,
        config: Config,
        client: LLMClient,
        invocation: dict[str, Any] | None = None,
    ) -> RunMetricsRecorder:
        """Capture the run baseline and nonconfiguration arguments before any model call."""
        started_at = _now_iso()
        started = time.perf_counter()
        input_info, input_signature = _capture_input_identity(input_path)
        return cls(
            operation=operation,
            requested_steps=list(requested_steps),
            input=input_info,
            config=config_identity(config),
            code=code_identity(),
            usage_before=client.usage_summary(),
            invocation=_safe_value(invocation or {}),
            started_at=started_at,
            _started=started,
            _input_signature=input_signature,
        )

    def verify_input_sha256(self, input_path: str) -> str | None:
        """Check the source cheaply; rehash changed signatures against the startup snapshot."""
        expected = self.input.get("sha256")
        if not isinstance(expected, str):
            return None
        path = Path(input_path)
        current_signature = _source_signature(path)
        # Windows st_ctime is the creation time, so same-size rewrites can keep this signature.
        if (
            os.name != "nt"
            and self._input_signature is not None
            and current_signature == self._input_signature
        ):
            return expected

        refreshed, signature = _capture_input_identity(input_path)
        actual = refreshed.get("sha256")
        if not isinstance(actual, str) or actual != expected:
            raise ValueError(
                "Source changed during this command; ensure the file is stable and retry."
            )
        self._input_signature = signature
        self.input.update(refreshed)
        return actual

    def attach_store(self, store: RunStore) -> None:
        """Bind the state directory, accepting only the first book actually used by this
        operation.
        """
        if self._store is None:
            self._store = store
            return
        if self._store.run_dir != store.run_dir:
            raise ValueError("A run ledger cannot span multiple book state directories")

    def capture_state(self, store: RunStore) -> None:
        """Freeze final state from live state or a snapshot whose consistency the caller
        guarantees.
        """
        self.attach_store(store)
        try:
            self._state_snapshot = _state_summary(store)
        except (OSError, KeyError, TypeError, ValueError):
            self._state_snapshot = {"available": False}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Accumulate stage wall time, counting nested stages with the same name only once."""
        depth = self._stage_depth.get(name, 0)
        self._stage_depth[name] = depth + 1
        if depth == 0:
            self._stage_started[name] = time.perf_counter()
        try:
            yield
        finally:
            remaining = self._stage_depth[name] - 1
            self._stage_depth[name] = remaining
            if remaining == 0:
                elapsed = time.perf_counter() - self._stage_started.pop(name)
                self._stage_seconds[name] = self._stage_seconds.get(name, 0.0) + elapsed

    def finish(
        self,
        client: LLMClient,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> str | None:
        """Finalize and persist the ledger without creating orphan records if state
        initialization failed.
        """
        if self._finished:
            return None
        self._finished = True
        if self._store is None:
            return None

        record: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "operation": self.operation,
            "requested_steps": self.requested_steps,
            "invocation": self.invocation,
            "status": status,
            "started_at": self.started_at,
            "stage_seconds": {
                name: round(seconds, 6) for name, seconds in sorted(self._stage_seconds.items())
            },
            "input": self.input,
            "config": self.config,
            "code": self.code,
            "usage": usage_delta(client.usage_summary(), self.usage_before),
        }
        if self._state_snapshot is not None:
            record["state"] = self._state_snapshot
        else:
            try:
                record["state"] = _state_summary(self._store)
            except (OSError, KeyError, TypeError, ValueError):
                record["state"] = {"available": False}
        if error is not None:
            record["error"] = {"type": type(error).__name__}
        record["finished_at"] = _now_iso()
        record["duration_seconds"] = round(time.perf_counter() - self._started, 6)
        return self._store.save_run_metric(record)
