"""Persistent read-only whole-book review records with resume support."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

from ..llm.usage import merge_usage_summaries


def review_candidate_id(
    chapter: int,
    chunk_base: int,
    ordinal: int,
    review_round: int | None = None,
) -> str:
    """Generate deterministic candidate IDs shared by initial snapshots and the agent protocol."""
    prefix = f"r{review_round}-" if review_round is not None else ""
    return f"{prefix}ch{chapter}-base{chunk_base}-candidate{ordinal}"


@dataclass(frozen=True)
class ReviewOutcome:
    """A completed review's result and directory."""

    run_dir: str
    result: dict[str, Any]
    usage: dict[str, Any]

    @property
    def issues(self) -> list[dict[str, Any]]:
        """Return issues remaining after blind rechecks."""
        return list(self.result.get("issues") or [])

    @property
    def changes(self) -> list[dict[str, Any]]:
        """Return collapsed final shadow-change recommendations."""
        return list(self.result.get("changes") or [])


class ReviewRunStore:
    """Manage results, events and round records for one read-only review."""

    def __init__(self, book_run_dir: str, *, now: datetime | None = None):
        moment = (now or datetime.now().astimezone()).astimezone()
        stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
        review_root = os.path.join(book_run_dir, "reviews")
        os.makedirs(review_root, exist_ok=True)

        candidate = os.path.join(review_root, f"review-{stamp}")
        suffix = 1
        while True:
            try:
                os.makedirs(candidate)
                break
            except FileExistsError:
                candidate = os.path.join(review_root, f"review-{stamp}-{suffix:02d}")
                suffix += 1

        self.run_dir = candidate
        self.review_id = os.path.basename(candidate)
        self.started_at = moment.isoformat(timespec="microseconds")
        self._event_path = os.path.join(candidate, "events.jsonl")
        self._event_lock = Lock()
        self._sequence = 0
        self._result_lock = Lock()
        self._initial_issues: list[dict[str, Any]] = []
        self._dismissed_issues: list[dict[str, Any]] = []
        self._active_round: int | None = None
        self._reviewed_content_digest = ""

    @staticmethod
    def _atomic_json(path: str, data: Any) -> None:
        """Write JSON atomically so interruption cannot leave a partial file."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def path(self, relative: str) -> str:
        """Return an absolute path within this review directory."""
        if self._active_round is not None:
            relative = f"rounds/{self._active_round:03d}/{relative}"
        return os.path.join(self.run_dir, relative)

    @contextmanager
    def round_scope(self, round_number: int) -> Iterator[None]:
        """Scope concurrent traces and stage artifacts to the specified review round."""
        if self._active_round is not None:
            raise RuntimeError("Review round scopes cannot be nested")
        self._active_round = round_number
        try:
            yield
        finally:
            self._active_round = None

    def write_json(self, relative: str, data: Any) -> str:
        """Atomically save round JSON and return its absolute path."""
        path = self.path(relative)
        self._atomic_json(path, data)
        return path

    def load_json(self, relative: str) -> dict[str, Any] | None:
        """Read round-scoped JSON, or return None if missing or damaged."""
        path = self.path(relative)
        try:
            with open(path, encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            return None

    def log_event(self, event: str, **data: Any) -> None:
        """Append structured review events under a lock."""
        if self._active_round is not None:
            data.setdefault("review_round", self._active_round)
        with self._event_lock:
            self._sequence += 1
            row = {
                "seq": self._sequence,
                "ts": datetime.now().astimezone().isoformat(timespec="microseconds"),
                "event": event,
                **data,
            }
            with open(self._event_path, "a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def record_initial_issues(
        self,
        *,
        chapter: int,
        chunk_base: int,
        issues: list[dict[str, Any]],
    ) -> None:
        """Aggregate initial candidates from successful leaf blocks under a lock."""
        rows = []
        for ordinal, issue in enumerate(issues):
            index = issue.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            rows.append(
                {
                    **dict(issue),
                    "candidate_id": review_candidate_id(
                        chapter,
                        chunk_base,
                        ordinal,
                        self._active_round,
                    ),
                    "chapter": chapter,
                    "index": chunk_base + index,
                    **(
                        {"review_round": self._active_round}
                        if self._active_round is not None
                        else {}
                    ),
                }
            )
        with self._result_lock:
            self._initial_issues.extend(rows)

    def record_dismissed(
        self,
        *,
        chapter: int,
        chunk_base: int,
        issues: list[dict[str, Any]],
    ) -> None:
        """Aggregate candidates dismissed by block agents under a lock."""
        rows = [
            {
                **dict(issue),
                "chapter": chapter,
                "index": chunk_base + int(issue["index"]),
                **({"review_round": self._active_round} if self._active_round is not None else {}),
            }
            for issue in issues
            if isinstance(issue.get("index"), int) and not isinstance(issue.get("index"), bool)
        ]
        with self._result_lock:
            self._dismissed_issues.extend(rows)

    def result_snapshots(
        self,
        round_number: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return copies of initial/dismissed issues ordered by round and book position."""
        with self._result_lock:
            initial = [
                dict(issue)
                for issue in self._initial_issues
                if round_number is None or issue.get("review_round") == round_number
            ]
            dismissed = [
                dict(issue)
                for issue in self._dismissed_issues
                if round_number is None or issue.get("review_round") == round_number
            ]

        def position(item: dict[str, Any]) -> tuple[Any, Any, Any]:
            return (
                item.get("review_round", -1),
                item.get("chapter", -1),
                item.get("index", -1),
            )

        return sorted(initial, key=position), sorted(dismissed, key=position)

    @staticmethod
    def is_resumable_status(status: object) -> bool:
        """Return True for Review statuses that may continue from saved caches."""
        return status in {"running", "interrupted"}

    def start(self, *, reviewed_content_digest: str, metadata: dict[str, Any]) -> None:
        """Create a running result and save parameters before the first model call.
        On resume with status=running/interrupted, preserve existing results and metadata
        instead of overwriting them.
        """
        self._reviewed_content_digest = reviewed_content_digest
        result_path = os.path.join(self.run_dir, "result.json")
        if os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if self.is_resumable_status(existing.get("status")):
                    # Resume: preserve results and metadata, updating only the timestamp.
                    existing["status"] = "running"
                    existing["termination"] = "running"
                    existing["resumed_at"] = (
                        datetime.now().astimezone().isoformat(timespec="microseconds")
                    )
                    existing.pop("finished_at", None)
                    existing.pop("interrupted_at", None)
                    existing.pop("last_error", None)
                    self._atomic_json(result_path, existing)
                    self.log_event("review_resumed", review_id=self.review_id)
                    return
            except (json.JSONDecodeError, OSError):
                pass

        # First run: save metadata and create a new result.
        metadata["reviewed_content_digest"] = reviewed_content_digest
        self.write_json("rounds/metadata.json", metadata)
        self._atomic_json(
            result_path,
            {
                "review_id": self.review_id,
                "status": "running",
                "termination": "not_started",
                "reviewed_content_digest": reviewed_content_digest,
                "started_at": self.started_at,
                "summary": {"issue_count": 0, "change_count": 0},
                "issues": [],
                "changes": [],
            },
        )
        self.log_event("review_started", review_id=self.review_id)

    def finish(
        self,
        *,
        status: str,
        termination: str,
        summary: dict[str, Any],
        issues: list[dict[str, Any]],
        changes: list[dict[str, Any]],
        error: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Persist the final unified result and return an in-memory copy."""
        result: dict[str, Any] = {
            "review_id": self.review_id,
            "status": status,
            "termination": termination,
            "reviewed_content_digest": self._reviewed_content_digest,
            "started_at": self.started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="microseconds"),
            "summary": dict(summary),
            "issues": list(issues),
            "changes": list(changes),
        }
        if error is not None:
            result["error"] = dict(error)
        self._atomic_json(os.path.join(self.run_dir, "result.json"), result)
        self.log_event(
            "review_finished",
            review_id=self.review_id,
            status=status,
            termination=termination,
            issue_count=len(issues),
            change_count=len(changes),
        )
        return result

    def mark_interrupted(
        self,
        *,
        error: dict[str, str] | None = None,
        summary: dict[str, Any] | None = None,
        issues: list[dict[str, Any]] | None = None,
        changes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist a recoverable pause while remaining eligible for find_resumable."""
        result_path = os.path.join(self.run_dir, "result.json")
        existing: dict[str, Any] = {}
        if os.path.isfile(result_path):
            try:
                with open(result_path, encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, OSError):
                existing = {}
        now = datetime.now().astimezone().isoformat(timespec="microseconds")
        result: dict[str, Any] = {
            "review_id": self.review_id,
            "status": "interrupted",
            "termination": "interrupted",
            "reviewed_content_digest": existing.get(
                "reviewed_content_digest", self._reviewed_content_digest
            ),
            "started_at": existing.get("started_at", self.started_at),
            "interrupted_at": now,
            "summary": dict(summary if summary is not None else existing.get("summary") or {}),
            "issues": list(issues if issues is not None else existing.get("issues") or []),
            "changes": list(changes if changes is not None else existing.get("changes") or []),
        }
        if error is not None:
            result["last_error"] = dict(error)
        elif isinstance(existing.get("last_error"), dict):
            result["last_error"] = dict(existing["last_error"])
        self._atomic_json(result_path, result)
        self.log_event(
            "review_interrupted",
            review_id=self.review_id,
            status="interrupted",
            error_type=(error or {}).get("type"),
            issue_count=len(result["issues"]),
            change_count=len(result["changes"]),
        )
        return result

    def save_usage(self, usage: dict[str, Any]) -> None:
        """Merge and persist review token deltas without loss across process resumes."""
        existing = self.load_usage()
        if existing is not None:
            usage = merge_usage_summaries(existing, usage)
        self._atomic_json(os.path.join(self.run_dir, "usage.json"), usage)
        self.log_event("review_usage_recorded", **usage["totals"])

    def load_usage(self) -> dict[str, Any] | None:
        """Read persisted review usage, or return None if absent."""
        path = os.path.join(self.run_dir, "usage.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    # Resume: chunk completion state.

    def mark_chunk_done(self, chunk_id: str, result: dict[str, Any]) -> None:
        """Mark a review block complete and cache its result for resume."""
        chunks_dir = os.path.join(self.run_dir, "chunks")
        os.makedirs(chunks_dir, exist_ok=True)
        self._atomic_json(os.path.join(chunks_dir, f"{chunk_id}.json"), result)

    def load_chunk_result(self, chunk_id: str) -> dict[str, Any] | None:
        """Load a completed block result, or return None if unfinished."""
        path = os.path.join(self.run_dir, "chunks", f"{chunk_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def is_chunk_done(self, chunk_id: str) -> bool:
        """Check whether a review block completed."""
        return self.load_chunk_result(chunk_id) is not None

    def rebuild_snapshots_from_chunks(self, review_round: int) -> None:
        """Rebuild initial/dismissed snapshots from persisted chunk caches.
        On scan_done resume, review_once no longer replays aggregation calls. Restore them
        from chunk files so final reports remain complete. Process larger blocks first and
        skip child blocks fully contained in an already recorded parent, preventing
        duplicate counts from stale split leaves.
        """
        chunks_dir = os.path.join(self.run_dir, "chunks")
        if not os.path.isdir(chunks_dir):
            return
        prefix = f"r{review_round}-ch"
        entries: list[tuple[int, int, int, dict[str, Any]]] = []
        for name in os.listdir(chunks_dir):
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            cached = self.load_chunk_result(name[:-5])
            if cached is None:
                continue
            tail = name[len(prefix) :]
            try:
                chapter_part, rest = tail.split("-base", 1)
                chapter = int(chapter_part)
                base_part, n_part = rest.split("-n", 1)
                chunk_base = int(base_part)
                size = int(n_part[:-5])
            except ValueError:
                continue
            entries.append((chapter, chunk_base, size, cached))
        # Sort by chapter, descending size and base so child containment can be detected.
        entries.sort(key=lambda e: (e[0], -e[2], e[1]))
        covered: dict[int, list[tuple[int, int]]] = {}
        for chapter, chunk_base, size, cached in entries:
            start, end = chunk_base, chunk_base + size
            ranges = covered.setdefault(chapter, [])
            if any(lo <= start and end <= hi for lo, hi in ranges):
                continue
            ranges.append((start, end))
            initial_issues = cached.get("initial_issues", [])
            if initial_issues:
                self.record_initial_issues(
                    chapter=chapter,
                    chunk_base=chunk_base,
                    issues=initial_issues,
                )
            dismissed = cached.get("dismissed", [])
            if dismissed:
                self.record_dismissed(
                    chapter=chapter,
                    chunk_base=chunk_base,
                    issues=dismissed,
                )

    # Resume: round checkpoints.

    def save_checkpoint(self, state: dict[str, Any]) -> None:
        """Save a round checkpoint for restoring the review loop."""
        self._atomic_json(os.path.join(self.run_dir, "checkpoint.json"), state)

    def load_checkpoint(self) -> dict[str, Any] | None:
        """Load the round checkpoint, or return None if absent."""
        path = os.path.join(self.run_dir, "checkpoint.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def find_resumable(
        book_run_dir: str,
        content_digest: str | None = None,
        *,
        config: dict[str, Any] | None = None,
        glossary_fingerprint: str | None = None,
    ) -> "ReviewRunStore | None":
        """Find the latest unfinished review to resume, or return None.
        When supplied, content_digest, config and glossary_fingerprint must match its
        metadata so changed settings or terms cannot reuse stale caches.
        """
        review_root = os.path.join(book_run_dir, "reviews")
        if not os.path.isdir(review_root):
            return None
        candidates = sorted(
            (d for d in os.listdir(review_root) if d.startswith("review-")),
            reverse=True,
        )
        for name in candidates:
            run_dir = os.path.join(review_root, name)
            result_path = os.path.join(run_dir, "result.json")
            if not os.path.isfile(result_path):
                continue
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    result = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not ReviewRunStore.is_resumable_status(result.get("status")):
                continue
            need_meta = (
                content_digest is not None or config is not None or glossary_fingerprint is not None
            )
            if need_meta:
                meta_path = os.path.join(run_dir, "rounds", "metadata.json")
                if not os.path.isfile(meta_path):
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue
                if (
                    content_digest is not None
                    and meta.get("reviewed_content_digest") != content_digest
                ):
                    continue
                if config is not None and meta.get("config") != config:
                    continue
                if (
                    glossary_fingerprint is not None
                    and meta.get("glossary_fingerprint") != glossary_fingerprint
                ):
                    continue
            return ReviewRunStore._from_existing(run_dir, name)
        return None

    @classmethod
    def _from_existing(cls, run_dir: str, review_id: str) -> "ReviewRunStore":
        """Restore a ReviewRunStore from an existing directory."""
        inst = cls.__new__(cls)
        inst.run_dir = run_dir
        inst.review_id = review_id
        inst._event_path = os.path.join(run_dir, "events.jsonl")
        inst._event_lock = Lock()
        inst._result_lock = Lock()
        inst._initial_issues = []
        inst._dismissed_issues = []
        inst._active_round = None
        inst._reviewed_content_digest = ""
        # Restore event sequence numbering to avoid collisions with existing events.
        inst._sequence = cls._read_max_seq(inst._event_path)
        # Restore started_at from result.json, leaving it empty if absent.
        inst.started_at = ""
        result_path = os.path.join(run_dir, "result.json")
        if os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                inst.started_at = existing.get("started_at", "")
            except (json.JSONDecodeError, OSError):
                pass
        return inst

    @classmethod
    def open_existing(cls, run_dir: str) -> "ReviewRunStore":
        """Open existing review storage so autofix shares its atomic writes and event sequence."""
        normalized = os.path.normpath(run_dir)
        review_id = os.path.basename(normalized)
        if not review_id.startswith("review-") or not os.path.isdir(normalized):
            raise ValueError("Invalid review directory")
        return cls._from_existing(normalized, review_id)

    @staticmethod
    def _read_max_seq(event_path: str) -> int:
        """Read the maximum sequence value from events.jsonl."""
        max_seq = 0
        if not os.path.isfile(event_path):
            return max_seq
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        seq = row.get("seq", 0)
                        if isinstance(seq, int) and seq > max_seq:
                            max_seq = seq
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            pass
        return max_seq
