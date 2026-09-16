"""Persistent read-only whole-book review records with resume support."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from threading import Lock
from typing import Any

from ..llm.usage import merge_usage_summaries
from ..storage.artifacts import FileArtifacts
from .models import review_candidate_id


class ReviewRunStore:
    """Manage results, events and round records for one read-only review."""

    def __init__(self, book_run_dir: str, *, now: datetime | None = None, storage=None):
        moment = (now or datetime.now().astimezone()).astimezone()
        stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
        self._storage = storage or FileArtifacts(book_run_dir)
        self._book_run_dir = book_run_dir
        review_root = os.path.join(book_run_dir, "reviews")
        candidate = os.path.join(review_root, f"review-{stamp}")
        suffix = 1
        while self._storage.list_artifacts(f"reviews/{os.path.basename(candidate)}/"):
            candidate = os.path.join(review_root, f"review-{stamp}-{suffix:02d}")
            suffix += 1
        self._storage.write_artifact(
            f"reviews/{os.path.basename(candidate)}/created.json",
            {"created_at": moment.isoformat()},
        )
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

    def _atomic_json(self, path: str, data: Any) -> None:
        """Write JSON atomically so interruption cannot leave a partial file."""
        self._storage.write_artifact(self._key(path), data)

    def _key(self, path: str) -> str:
        return os.path.relpath(path, self._book_run_dir).replace(os.sep, "/")

    def _read(self, path: str) -> Any | None:
        return self._storage.read_artifact(self._key(path))

    def _names(self, relative: str) -> list[str]:
        prefix = f"reviews/{self.review_id}/{relative.strip('/')}/"
        return [
            key[len(prefix) :]
            for key in self._storage.list_artifacts(prefix)
            if "/" not in key[len(prefix) :]
        ]

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
        """Read round-scoped JSON through the injected artifact backend."""
        return self._read(self.path(relative))

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
            self._storage.append_artifact_record(self._key(self._event_path), row)

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
        """Return True for unfinished Review statuses that may continue from saved caches.

        ``failed`` stays resume-eligible so protocol/local errors can reuse chunk caches;
        failure details remain in result/event logs for diagnosis.
        """
        return status in {"running", "interrupted", "failed"}

    def start(self, *, reviewed_content_digest: str, metadata: dict[str, Any]) -> None:
        """Create a running result and save parameters before the first model call.
        On resume with status=running/interrupted/failed, preserve existing results and
        metadata instead of overwriting them.
        """
        self._reviewed_content_digest = reviewed_content_digest
        result_path = os.path.join(self.run_dir, "result.json")
        existing = self._read(result_path)
        if isinstance(existing, dict) and self.is_resumable_status(existing.get("status")):
            existing["status"] = "running"
            existing["termination"] = "running"
            existing["resumed_at"] = datetime.now().astimezone().isoformat(timespec="microseconds")
            for field in ("finished_at", "interrupted_at", "last_error", "error"):
                existing.pop(field, None)
            self._atomic_json(result_path, existing)
            self.log_event("review_resumed", review_id=self.review_id)
            return

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
        existing = self._read(result_path) or {}
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
        return self._read(os.path.join(self.run_dir, "usage.json"))

    def mark_chunk_done(self, chunk_id: str, result: dict[str, Any]) -> None:
        """Mark a review block complete and cache its result for resume."""
        chunks_dir = os.path.join(self.run_dir, "chunks")
        self._atomic_json(os.path.join(chunks_dir, f"{chunk_id}.json"), result)

    def load_chunk_result(self, chunk_id: str) -> dict[str, Any] | None:
        return self._read(os.path.join(self.run_dir, "chunks", f"{chunk_id}.json"))

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
        prefix = f"r{review_round}-ch"
        entries: list[tuple[int, int, int, dict[str, Any]]] = []
        for name in self._names("chunks"):
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
        return self._read(os.path.join(self.run_dir, "checkpoint.json"))

    @staticmethod
    def find_resumable(
        book_run_dir: str,
        content_digest: str | None = None,
        *,
        config: dict[str, Any] | None = None,
        glossary_fingerprint: str | None = None,
        storage=None,
    ) -> "ReviewRunStore | None":
        backend = storage or FileArtifacts(book_run_dir)
        for name in ReviewRunStore.list_review_ids(backend):
            result = backend.read_artifact(f"reviews/{name}/result.json")
            if not isinstance(result, dict) or not ReviewRunStore.is_resumable_status(
                result.get("status")
            ):
                continue
            if content_digest is not None or config is not None or glossary_fingerprint is not None:
                meta = backend.read_artifact(f"reviews/{name}/rounds/metadata.json")
                if not isinstance(meta, dict):
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
            return ReviewRunStore._from_existing(
                os.path.join(book_run_dir, "reviews", name), name, storage=backend
            )
        return None

    @staticmethod
    def list_review_ids(storage) -> list[str]:
        return sorted(
            {
                key.split("/")[1]
                for key in storage.list_artifacts("reviews/")
                if len(key.split("/")) > 2 and key.split("/")[1].startswith("review-")
            },
            reverse=True,
        )

    @classmethod
    def _from_existing(cls, run_dir: str, review_id: str, *, storage=None) -> "ReviewRunStore":
        inst = cls.__new__(cls)
        inst.run_dir = run_dir
        inst.review_id = review_id
        inst._book_run_dir = os.path.dirname(os.path.dirname(run_dir))
        inst._storage = storage or FileArtifacts(inst._book_run_dir)
        inst._event_path = os.path.join(run_dir, "events.jsonl")
        inst._event_lock = Lock()
        inst._result_lock = Lock()
        inst._initial_issues = []
        inst._dismissed_issues = []
        inst._active_round = None
        inst._reviewed_content_digest = ""
        records = inst._storage.read_artifact_records(inst._key(inst._event_path))
        inst._sequence = max(
            (row.get("seq", 0) for row in records if isinstance(row.get("seq"), int)), default=0
        )
        existing = inst._read(os.path.join(run_dir, "result.json")) or {}
        inst.started_at = existing.get("started_at", "")
        return inst

    @classmethod
    def open_existing(cls, run_dir: str, *, storage=None) -> "ReviewRunStore":
        normalized = os.path.normpath(run_dir)
        review_id = os.path.basename(normalized)
        backend = storage or FileArtifacts(os.path.dirname(os.path.dirname(normalized)))
        if not review_id.startswith("review-") or not backend.list_artifacts(
            f"reviews/{review_id}/"
        ):
            raise ValueError("Invalid review directory")
        return cls._from_existing(normalized, review_id, storage=backend)

    @staticmethod
    def _read_max_seq(event_path: str) -> int:
        backend = FileArtifacts(os.path.dirname(event_path))
        rows = backend.read_artifact_records(os.path.basename(event_path))
        return max(
            (row.get("seq", 0) for row in rows if isinstance(row.get("seq"), int)), default=0
        )
