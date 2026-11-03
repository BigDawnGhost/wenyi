"""Persistent run state with interruption recovery.

This is the storage layer, not pipeline orchestration: it owns the run-directory layout,
atomic JSON writes, the run/state/event/assemble locks and the interruption-recovery
protocol. It lives under `storage/` so that `storage/` never imports `pipeline/` for its
own primitives; `pipeline/runstore.py` keeps the public import path as a forwarding shell.

Within the selected book/target run directory: manifest.json stores book metadata and
chapter status; chapters/ch{n}.json stores segments; source/ caches preprocessing;
annotation_contexts.json indexes immutable EPUB annotation sources;
analysis.json stores global analysis. Recent translations are derived from chapters.
usage.json accumulates tokens across runs; glossary.db stores terms/conflicts;
events.jsonl is append-only; reviews/ holds review results, round records and optional
autofix publication indices.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from threading import local
from typing import Any

from ..i18n.languages import require_language
from ..ingest.source_hash import source_sha256
from ..models.document import Chapter, Document
from ..timing import save_timing
from .artifacts import FileArtifacts
from .atomic_write import sweep_temporary, write_text_atomic
from .io_path import to_io_path
from .locks import (
    ASSEMBLE_LOCK,
    EVENTS_LOCK,
    RUN_LOCK,
    STATE_LOCK,
    TIMING_LOCK,
    exclusive_file_lock,
)
from .names import DEFAULT_COMPONENT, escape_reserved, strip_trailing

STATUS_PENDING = "pending"
STATUS_DONE = "done"


def slugify(name: str) -> str:
    """Convert a book title into a stable short state-directory name."""
    s = re.sub(r"[^\w一-鿿぀-ヿ-]+", "_", name).strip("_")
    # The reserved-name escape prefixes an underscore, so it has to come after
    # the separator trim that would otherwise remove it again.
    return escape_reserved(strip_trailing(s)) or DEFAULT_COMPONENT


def translation_run_dir(state_dir: str, title: str, target_lang: str) -> str:
    """Use the same target-isolated layout for every translation language."""
    target = require_language(target_lang)
    return os.path.join(state_dir, slugify(title), "targets", target)


class RunStore(FileArtifacts):
    def __init__(self, run_dir: str, *, create: bool = True):
        """Bind a book state directory and optionally create chapter subdirectories."""
        FileArtifacts.__init__(self, run_dir)
        self.run_dir = run_dir
        self.chapters_dir = os.path.join(run_dir, "chapters")
        self._batch_glossary_event_cache: dict[int, set[str]] | None = None
        self._state_local = local()
        if create:
            self.ensure_dirs()

    def ensure_dirs(self) -> None:
        """Create run and chapter-state directories."""
        os.makedirs(to_io_path(self.chapters_dir), exist_ok=True)

    @contextmanager
    def _file_lock(self, filename: str) -> Iterator[None]:
        """Serialize cross-process operations using the named lock file within state."""
        self.ensure_dirs()
        with exclusive_file_lock(to_io_path(os.path.join(self.run_dir, filename))):
            yield

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize long-running translation and review operations for one book."""
        with self._file_lock(RUN_LOCK):
            yield

    @contextmanager
    def state_lock(self) -> Iterator[None]:
        """Briefly freeze manifest and chapters for atomic persistence or a consistent
        snapshot. Nested calls on the same store/thread reuse the outer lock; independent
        threads and processes still acquire the OS-level lock.
        """
        if getattr(self._state_local, "locked", False):
            yield
            return
        with self._file_lock(STATE_LOCK):
            self._state_local.locked = True
            try:
                yield
            finally:
                self._state_local.locked = False

    @contextmanager
    def event_lock(self) -> Iterator[None]:
        """Serialize JSONL event appends so concurrent commands cannot interleave one line."""
        with self._file_lock(EVENTS_LOCK):
            yield

    @contextmanager
    def assemble_lock(self) -> Iterator[None]:
        """Serialize book artifact writes without blocking ongoing body translation."""
        with self._file_lock(ASSEMBLE_LOCK):
            yield

    def record_timing(self, record: dict[str, Any]) -> dict[str, Any]:
        """Merge invocation timing under a dedicated lock, independent of exports."""
        with self._file_lock(TIMING_LOCK):
            return save_timing(self.run_dir, record)

    # Paths.
    @property
    def manifest_path(self) -> str:
        """Return the book manifest path."""
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def initialization_path(self) -> str:
        """Return the temporary source-identity path for incomplete initialization."""
        return os.path.join(self.run_dir, ".initializing.json")

    @property
    def annotation_contexts_path(self) -> str:
        """Return the EPUB annotation-context index path."""
        return os.path.join(self.run_dir, "annotation_contexts.json")

    @property
    def analysis_path(self) -> str:
        """Return the whole-book style-analysis path."""
        return os.path.join(self.run_dir, "analysis.json")

    @property
    def glossary_path(self) -> str:
        """Return the glossary and translation-conflict database path."""
        return os.path.join(self.run_dir, "glossary.db")

    @property
    def usage_path(self) -> str:
        """Return the cumulative book token-usage path."""
        return os.path.join(self.run_dir, "usage.json")

    @property
    def event_log_path(self) -> str:
        """Return the append-only JSONL event-log path."""
        return os.path.join(self.run_dir, "events.jsonl")

    def chapter_path(self, ci: int) -> str:
        """Return the state-file path for a chapter index."""
        return os.path.join(self.chapters_dir, f"ch{ci}.json")

    @property
    def source_dir(self) -> str:
        """Return the preprocessing cache directory; readers create it when needed."""
        return os.path.join(self.run_dir, "source")

    @property
    def reviews_dir(self) -> str:
        """Return the directory holding read-only whole-book review runs."""
        return os.path.join(self.run_dir, "reviews")

    # Shared JSON helpers.
    @staticmethod
    def _write_json(path: str, data) -> None:
        """Write formatted JSON atomically through a temporary file in the same directory."""
        write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def _read_json(path: str):
        """Read and parse UTF-8 JSON."""
        with open(to_io_path(path), "r", encoding="utf-8") as f:
            return json.load(f)

    def _exists(self, path: str) -> bool:
        """Check for one run file through the same namespace the writer used."""
        return os.path.isfile(to_io_path(path))

    def _remove(self, path: str) -> None:
        """Delete one run file, tolerating a file another process already removed."""
        try:
            os.remove(to_io_path(path))
        except FileNotFoundError:
            pass

    def _listdir(self, directory: str) -> list[str]:
        """List one run directory; a missing directory is an empty one."""
        try:
            return os.listdir(to_io_path(directory))
        except FileNotFoundError:
            return []

    def exists(self) -> bool:
        """Check whether initialization completed and committed a manifest."""
        return self._exists(self.manifest_path)

    def begin_initialization(self, source_hash: str) -> None:
        """Clear incomplete derived state and record the current source identity.
        Preserve expensive, hash-isolated PDF conversion caches under source/, plus failed
        events and cumulative usage for the same verified source. Recover its pending usage
        commit under the caller's run lock before rebuilding derived state.
        Rebuild mutable chapters, glossary and analysis
        so data left before a failed manifest commit cannot contaminate a new task.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("Invalid source SHA-256 format")

        previous_hash: str | None = None
        if self._exists(self.initialization_path):
            try:
                marker = self._read_json(self.initialization_path)
            except (OSError, json.JSONDecodeError, TypeError):
                marker = None
            if isinstance(marker, dict) and isinstance(marker.get("source_sha256"), str):
                previous_hash = marker["source_sha256"]

        if previous_hash == source_hash:
            self.recover_usage()
        else:
            # Discard the old journal first so interruption cannot replay it into a new run.
            for path in (os.path.join(self.run_dir, "usage-pending.json"), self.usage_path):
                self._remove(path)

        shutil.rmtree(to_io_path(self.chapters_dir), ignore_errors=True)
        os.makedirs(to_io_path(self.chapters_dir), exist_ok=True)
        for path in (
            self.analysis_path,
            self.annotation_contexts_path,
            self.glossary_path,
            f"{self.glossary_path}-wal",
            f"{self.glossary_path}-shm",
            f"{self.glossary_path}-journal",
        ):
            self._remove(path)

        if previous_hash != source_hash:
            shutil.rmtree(to_io_path(self.reviews_dir), ignore_errors=True)
            self._remove(self.event_log_path)

        # Every temporary file here belongs to an interrupted write of this run.
        # The sweep runs after the derived state is discarded so a temporary file
        # written before the crash cannot be mistaken for current state.
        self.sweep_temporary_files()
        self._batch_glossary_event_cache = None
        self._write_json(
            self.initialization_path,
            {"source_sha256": source_hash},
        )

    def sweep_temporary_files(self) -> list[str]:
        """Delete temporary files left by an interrupted write in the run directory.

        Only the names `write_text_atomic` produces and the legacy single
        `<name>.tmp` form it replaced are removed. The run directory also holds
        exported documents and cached sources, so matching every `*.tmp` would
        eventually delete a file this program never wrote.
        """
        return sweep_temporary(self.run_dir)

    def finish_initialization(self) -> None:
        """Remove temporary initialization identity after the manifest commits successfully."""
        self._remove(self.initialization_path)

    # ── manifest ──────────────────────────────────────────────────────────
    def stage_document(
        self,
        doc: Document,
        *,
        source_hash: str | None = None,
    ) -> dict:
        """Write initial chapters and return manifest data without committing it early.
        The caller saves the manifest last, after analysis, glossary and context, because it
        marks successful initialization.
        """
        digest = source_hash or source_sha256(doc.source_path)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid source SHA-256 format")
        document_meta = dict(doc.meta)
        annotation_contexts = document_meta.pop("epub_annotation_contexts", None)
        if isinstance(annotation_contexts, dict) and annotation_contexts:
            self.save_annotation_contexts(annotation_contexts)
        else:
            self._remove(self.annotation_contexts_path)

        manifest = {
            "title": doc.title,
            "fmt": doc.fmt,
            "source_sha256": digest,
            "source_lang": doc.source_lang,
            "target_lang": doc.target_lang,
            "meta": document_meta,
            "chapters": [
                {
                    "index": c.index,
                    "title": c.title,
                    "href": c.href,
                    "toc_entry_id": c.meta.get("toc_entry_id"),
                    "status": STATUS_PENDING,
                }
                for c in doc.chapters
            ],
        }
        for c in doc.chapters:
            self.save_chapter(c)
        return manifest

    def ensure_source_identity(
        self,
        input_path: str,
        *,
        actual_sha256: str | None = None,
    ) -> str:
        """Validate that input content belongs to this state; reject state lacking
        source identity.
        """
        actual = actual_sha256 or source_sha256(input_path)
        with self.state_lock():
            self._validate_source_identity(self.load_manifest(), actual)
        return actual

    @staticmethod
    def _validate_source_identity(manifest: dict, actual: str) -> None:
        """Validate source identity using an already loaded manifest."""
        if not re.fullmatch(r"[0-9a-f]{64}", actual):
            raise ValueError("Invalid source SHA-256 format")

        expected = manifest.get("source_sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(
                "Existing state has no valid source_sha256; prepare fresh state in a separate directory."
            )
        if expected != actual:
            raise ValueError(
                "Input content does not match existing translation state with the same directory name. Use the original source "
                "or prepare fresh state in a separate directory."
            )

    def create_export_snapshot(self, *, actual_sha256: str) -> ExportSnapshotStore:
        """Freeze the manifest and every chapter needed for export under the short state lock."""
        with self.state_lock():
            manifest = self.load_manifest()
            self._validate_source_identity(manifest, actual_sha256)
            chapter_entries = manifest.get("chapters")
            if not isinstance(chapter_entries, list):
                raise ValueError("Invalid chapter inventory in translation state")

            chapters: dict[int, Chapter] = {}
            for entry in chapter_entries:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid chapter entry in translation state")
                chapter_index = entry.get("index")
                if not isinstance(chapter_index, int) or isinstance(chapter_index, bool):
                    raise ValueError("Invalid chapter index in translation state")
                if chapter_index in chapters:
                    raise ValueError(
                        f"Duplicate chapter index in translation state: {chapter_index}"
                    )
                chapter = self.load_chapter(chapter_index)
                if chapter.index != chapter_index:
                    raise ValueError(f"Chapter file index mismatch: {chapter_index}")
                chapters[chapter_index] = chapter

        return ExportSnapshotStore(self.run_dir, manifest, chapters)

    def save_manifest(self, manifest: dict) -> None:
        """Save the manifest and chapter status atomically."""
        with self.state_lock():
            self._write_json(self.manifest_path, manifest)

    def load_manifest(self) -> dict:
        """Read the manifest and chapter status."""
        return self._read_json(self.manifest_path)

    def set_chapter_status(self, ci: int, status: str) -> None:
        """Update one chapter status and atomically save the complete manifest."""
        with self.state_lock():
            manifest = self.load_manifest()
            for c in manifest["chapters"]:
                if c["index"] == ci:
                    c["status"] = status
                    break
            self._write_json(self.manifest_path, manifest)

    def pending_chapters(self) -> list[int]:
        """Return chapter indices not yet marked complete."""
        manifest = self.load_manifest()
        return [c["index"] for c in manifest["chapters"] if c["status"] != STATUS_DONE]

    # Chapters.
    def save_chapter(self, chapter: Chapter) -> None:
        """Atomically save a chapter's source, target and stage metadata."""
        with self.state_lock():
            self._write_json(self.chapter_path(chapter.index), chapter.model_dump())

    def save_chapter_with_status(self, chapter: Chapter, status: str) -> None:
        """Publish final chapter content and its manifest status under the same state lock."""
        with self.state_lock():
            self._write_json(self.chapter_path(chapter.index), chapter.model_dump())
            manifest = self.load_manifest()
            for entry in manifest["chapters"]:
                if entry["index"] == chapter.index:
                    entry["status"] = status
                    break
            self._write_json(self.manifest_path, manifest)

    def load_chapter(self, ci: int) -> Chapter:
        """Read and validate chapter state."""
        return Chapter.model_validate(self._read_json(self.chapter_path(ci)))

    # Annotation contexts and analysis.
    def save_annotation_contexts(self, data: dict) -> None:
        """Atomically save the deduplicated EPUB annotation-source index."""
        with self.state_lock():
            self._write_json(self.annotation_contexts_path, data)

    def load_annotation_contexts(self) -> dict | None:
        """Read EPUB annotation context, or return None for non-EPUB/missing indices."""
        return (
            self._read_json(self.annotation_contexts_path)
            if self._exists(self.annotation_contexts_path)
            else None
        )

    def save_analysis(self, data: dict) -> None:
        """Atomically save book analysis and synopsis data under the state lock."""
        with self.state_lock():
            self._write_json(self.analysis_path, data)

    def load_analysis(self) -> dict | None:
        """Read book analysis, or return None if absent."""
        return self._read_json(self.analysis_path) if self._exists(self.analysis_path) else None

    def save_usage(self, data: dict) -> None:
        """Atomically save cumulative token usage under the state lock."""
        with self.state_lock():
            self._write_json(self.usage_path, data)

    def load_usage(self) -> dict | None:
        """Read cumulative token usage, or return None if absent."""
        return self._read_json(self.usage_path) if self._exists(self.usage_path) else None

    def prepare_usage_commit(self, ledgers: dict[str, dict]) -> None:
        """Journal complete ledger snapshots before publication, under the book run lock."""
        from ..llm.routing import identity

        entries = []
        for relative, value in ledgers.items():
            path = self._usage_commit_path(relative)
            before = self._read_json(path) if self._exists(path) else None
            entries.append({"path": relative, "before": identity(before), "value": value})
        self._write_json(
            os.path.join(self.run_dir, "usage-pending.json"), {"version": 1, "entries": entries}
        )

    def _usage_commit_path(self, relative: str) -> str:
        """Restrict journal destinations to ledgers in this run, never arbitrary state."""
        parts = relative.replace("\\", "/").split("/")
        if relative != "usage.json" and not (
            len(parts) == 3
            and parts[0] == "reviews"
            and parts[1].startswith("review-")
            and parts[2] == "usage.json"
        ):
            raise ValueError("Invalid usage journal destination")
        return os.path.join(self.run_dir, *parts)

    def recover_usage(self) -> None:
        """Idempotently finish an interrupted book/review ledger commit under the run lock."""
        from ..llm.routing import identity
        from ..llm.usage import validate_usage

        pending = os.path.join(self.run_dir, "usage-pending.json")
        if not self._exists(pending):
            return
        transaction = self._read_json(pending)
        if transaction.get("version") != 1 or not isinstance(transaction.get("entries"), list):
            raise ValueError("Invalid usage journal")
        writes = []
        for entry in transaction["entries"]:
            path = self._usage_commit_path(entry["path"])
            value = validate_usage(entry["value"])
            current = self._read_json(path) if self._exists(path) else None
            if identity(current) not in {entry["before"], identity(value)}:
                raise ValueError("Usage ledger changed outside its pending commit")
            writes.append((path, value))
        for path, value in writes:
            self._write_json(path, value)
        os.unlink(to_io_path(pending))

    def load_latest_review_result(self) -> dict[str, Any] | None:
        """Read the latest completed or failed review result by directory time order."""
        for name in sorted(self._listdir(self.reviews_dir), reverse=True):
            if not name.startswith("review-"):
                continue
            path = os.path.join(self.reviews_dir, name, "result.json")
            if not self._exists(path):
                continue
            try:
                result = self._read_json(path)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if isinstance(result, dict):
                return result
        return None

    # Batch recovery checkpoints.
    @staticmethod
    def batch_glossary_key(start_index: int, count: int) -> str:
        """Return a glossary-extraction checkpoint key that changes when batch boundaries
        change.
        """
        return f"{start_index}:{count}"

    def completed_batch_glossary_keys(self, chapter: int) -> set[str]:
        """Restore completed batch extraction from events, scanning at most once per store
        instance.
        """
        if self._batch_glossary_event_cache is None:
            completed: dict[int, set[str]] = {}
            if self._exists(self.event_log_path):
                with open(to_io_path(self.event_log_path), "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if row.get("event") != "batch_glossary_extracted":
                            continue
                        ci = row.get("chapter")
                        start = row.get("start_index")
                        count = row.get("count")
                        if not (
                            isinstance(ci, int)
                            and isinstance(start, int)
                            and isinstance(count, int)
                        ):
                            continue
                        completed.setdefault(ci, set()).add(self.batch_glossary_key(start, count))
            self._batch_glossary_event_cache = completed
        return set(self._batch_glossary_event_cache.get(chapter, set()))

    # Append-only event log.
    def log_event(self, event: str, **data: Any) -> None:
        """Append a JSONL event for translation actions, before/after changes and artifact
        accounting.
        """
        self.ensure_dirs()
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        with self.event_lock():
            with open(to_io_path(self.event_log_path), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        if event == "batch_glossary_extracted" and self._batch_glossary_event_cache is not None:
            chapter = data.get("chapter")
            start = data.get("start_index")
            count = data.get("count")
            if isinstance(chapter, int) and isinstance(start, int) and isinstance(count, int):
                self._batch_glossary_event_cache.setdefault(chapter, set()).add(
                    self.batch_glossary_key(start, count)
                )


class ExportSnapshotStore(RunStore):
    """Read-only in-memory export state; source resources still refer to the formal run
    directory.
    """

    def __init__(
        self,
        run_dir: str,
        manifest: dict,
        chapters: dict[int, Chapter],
    ) -> None:
        super().__init__(run_dir, create=False)
        self._snapshot_manifest = deepcopy(manifest)
        self._snapshot_chapters = {
            index: chapter.model_copy(deep=True) for index, chapter in chapters.items()
        }

    def load_manifest(self) -> dict:
        """Return an independent copy of the frozen manifest."""
        return deepcopy(self._snapshot_manifest)

    def load_chapter(self, ci: int) -> Chapter:
        """Return an independent frozen-chapter copy so one render cannot affect later outputs."""
        try:
            chapter = self._snapshot_chapters[ci]
        except KeyError as error:
            raise FileNotFoundError(f"Chapter not found in snapshot: {ci}") from error
        return chapter.model_copy(deep=True)
