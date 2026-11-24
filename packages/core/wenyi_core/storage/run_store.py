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

import errno
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from threading import local
from typing import Any

from ..i18n.languages import require_language, validate_run_languages
from ..ingest.source_hash import source_sha256
from ..models.document import Chapter, Document
from ..timing import save_timing
from .artifacts import FileArtifacts
from .atomic_write import sweep_temporary, write_text_atomic
from .chapter_publication import JOURNAL_NAME, ChapterPublication
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
_MANIFEST_NAME = "manifest.json"
_INITIALIZING_NAME = ".initializing.json"


#: Cap for the slug component so "state/<slug>/targets/<lang>/..." stays well
#: below the Windows MAX_PATH limit even for very long titles.
SLUG_MAX_LENGTH = 64
_SLUG_SUFFIX_LENGTH = 8


def slugify(name: str) -> str:
    """Convert a book title into a stable short state-directory name.

    Slugs longer than SLUG_MAX_LENGTH are truncated and disambiguated with a
    deterministic sha256 suffix, so the same title always maps to the same
    directory and long titles sharing a prefix never collide.
    """
    return _cap_slug(_uncapped_slug(name))


def _uncapped_slug(name: str) -> str:
    s = re.sub(r"[^\w一-鿿぀-ヿ-]+", "_", name).strip("_")
    # The reserved-name escape prefixes an underscore, so it has to come after
    # the separator trim that would otherwise remove it again.
    return escape_reserved(strip_trailing(s)) or DEFAULT_COMPONENT


def _cap_slug(slug: str) -> str:
    """Truncate an over-long slug and append a short sha256 suffix for uniqueness."""
    if len(slug) <= SLUG_MAX_LENGTH:
        return slug
    suffix = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:_SLUG_SUFFIX_LENGTH]
    stem = slug[: SLUG_MAX_LENGTH - _SLUG_SUFFIX_LENGTH - 1].rstrip("_") or DEFAULT_COMPONENT
    return f"{stem}-{suffix}"


def translation_run_dir(state_dir: str, title: str, target_lang: str) -> str:
    """Use the same target-isolated layout for every translation language."""
    target = require_language(target_lang)
    return os.path.join(state_dir, slugify(title), "targets", target)


def resolve_file_run_dir(
    state_dir: str,
    title: str,
    target_lang: str,
    *,
    source_lang: str,
    actual_sha256: str,
) -> str:
    """Select one CLI target directory, without opening a store or changing either path."""
    target = require_language(target_lang)
    short = translation_run_dir(state_dir, title, target)
    legacy = os.path.join(state_dir, _uncapped_slug(title), "targets", target)
    use_legacy = legacy != short and _run_dir_exists(legacy, legacy_probe=True)
    if use_legacy and _run_dir_exists(short):
        raise ValueError(
            f"Both short and legacy translation state directories exist for this target:\n"
            f"  {short}\n  {legacy}\n"
            "Back up both and keep only the state you intend to use in paths.state_dir. "
            "Wenyi will not merge them."
        )

    run_dir = legacy if use_legacy else short
    manifest = _read_run_identity(run_dir, _MANIFEST_NAME)
    if manifest is not None:
        validate_run_languages(manifest, source_lang, target_lang)
        RunStore._validate_source_identity(manifest, actual_sha256)
        return run_dir

    if not use_legacy:
        # The normal initialization path can already rebuild abandoned derived state.
        return run_dir

    # Require proof before taking over an old path. The initialization marker's target
    # language is encoded by the target directory, not the marker.
    marker = _read_run_identity(run_dir, _INITIALIZING_NAME)
    if marker is not None:
        RunStore._validate_source_identity(marker, actual_sha256)
        return run_dir

    # `parse` can leave a source cache without having started initialization.
    parsed = _read_run_identity(run_dir, "parsed_document.json")
    if parsed is None:
        raise ValueError(
            f"Legacy translation state has no saved source identity: {run_dir}. "
            "Use a separate paths.state_dir to start fresh."
        )
    RunStore._validate_source_identity(parsed, actual_sha256)
    ingest = parsed.get("ingest_config")
    if not isinstance(ingest, dict) or ingest.get("target_lang") != target:
        raise ValueError(
            f"Saved target language in parsed state does not match requested {target_lang}: "
            f"{run_dir}. Use the matching language.target or a separate paths.state_dir."
        )
    return run_dir


def _run_dir_exists(path: str, *, legacy_probe: bool = False) -> bool:
    try:
        mode = os.stat(to_io_path(path)).st_mode
    except FileNotFoundError:
        return False
    except OSError as error:
        # A legacy slug that cannot be represented on this filesystem never existed here.
        if legacy_probe and (
            error.errno == errno.ENAMETOOLONG or getattr(error, "winerror", None) == 206
        ):
            return False
        raise
    if not stat.S_ISDIR(mode):
        raise ValueError(f"Translation state path is not a directory: {path}")
    return True


def _read_run_identity(run_dir: str, filename: str) -> dict | None:
    path = os.path.join(run_dir, filename)
    try:
        data = RunStore._read_json(path)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"Cannot read saved translation state identity: {path}") from error
    if not isinstance(data, dict):
        raise ValueError(f"Cannot read saved translation state identity: {path}")
    return data


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
            self.clear_stale_initialization()

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
            self._state_local.run_locked = True
            try:
                yield
            finally:
                self._state_local.run_locked = False

    @contextmanager
    def state_lock(self) -> Iterator[None]:
        """Briefly freeze manifest and chapters for atomic persistence or a consistent
        snapshot. Nested calls on the same store/thread reuse the outer lock; independent
        threads and processes still acquire the OS-level lock.
        """
        with self._state_lock(create=True):
            yield

    @contextmanager
    def _state_lock(self, *, create: bool) -> Iterator[None]:
        if getattr(self._state_local, "locked", False):
            yield
            return
        if create:
            os.makedirs(to_io_path(self.run_dir), exist_ok=True)
        with exclusive_file_lock(to_io_path(os.path.join(self.run_dir, STATE_LOCK))):
            self._state_local.locked = True
            try:
                yield
            finally:
                self._state_local.__dict__.pop("publication", None)
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
        return os.path.join(self.run_dir, _MANIFEST_NAME)

    @property
    def initialization_path(self) -> str:
        """Return the temporary source-identity path for incomplete initialization."""
        return os.path.join(self.run_dir, _INITIALIZING_NAME)

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
    def _json_text(data) -> str:
        """Serialize run state the way every JSON file in the run directory is written."""
        return json.dumps(data, ensure_ascii=False, indent=2)

    @classmethod
    def _write_json(cls, path: str, data) -> None:
        """Write formatted JSON atomically through a temporary file in the same directory."""
        write_text_atomic(path, cls._json_text(data))

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
        return self._exists(self.manifest_path) or self._exists(self._publication_path)

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
        with self.state_lock():
            if self._read_chapter_publication() is not None:
                raise ValueError("Cannot initialize state with a pending chapter publication")
            self._begin_initialization(source_hash)

    def _begin_initialization(self, source_hash: str) -> None:
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

    def clear_stale_initialization(self) -> bool:
        """Drop an initialization marker left behind by a completed initialization.

        A crash between the manifest commit and `finish_initialization` leaves
        `.initializing.json` in place even though initialization succeeded, and
        resumed runs never touch it. The committed manifest is the completion
        authority, so such a marker is removable garbage. Do not change files
        while chapter publication needs recovery. A marker without a manifest
        carries the identity needed to recover pending initialization usage.
        """
        if not self._exists(self.initialization_path):
            return False
        with self.state_lock():
            if not self._exists(self.manifest_path) or self._exists(self._publication_path):
                return False
            self._remove(self.initialization_path)
            return True

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

        manifest = self._document_manifest(doc, digest, document_meta)
        for c in doc.chapters:
            self.save_chapter(c)
        return manifest

    @staticmethod
    def _document_manifest(doc: Document, digest: str, document_meta: dict) -> dict:
        return {
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

    def ensure_source_identity(
        self,
        input_path: str,
        *,
        actual_sha256: str | None = None,
        recover_if_run_locked: bool = False,
    ) -> str:
        """Validate that input content belongs to this state; reject state lacking
        source identity. A modifying workflow can finish chapter recovery after validation.
        """
        actual = actual_sha256 or source_sha256(input_path)
        with self.state_lock():
            self._validate_source_identity(self.load_manifest(), actual)
            if recover_if_run_locked and getattr(self._state_local, "run_locked", False):
                self._recover_chapter_publication()
        return actual

    @staticmethod
    def _validate_source_identity(manifest: dict, actual: str) -> None:
        """Validate source identity using an already loaded manifest."""
        if not isinstance(actual, str) or not re.fullmatch(r"[0-9a-f]{64}", actual):
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
            self._recover_chapter_publication()
            self._write_json(self.manifest_path, manifest)

    def load_manifest(self) -> dict:
        """Read the manifest and chapter status."""
        return self._read_formal_state()

    def set_chapter_status(self, ci: int, status: str) -> None:
        """Update one chapter status and atomically save the complete manifest."""
        with self.state_lock():
            self._recover_chapter_publication()
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
            self._recover_chapter_publication()
            self._write_json(self.chapter_path(chapter.index), chapter.model_dump())

    def save_chapter_with_status(self, chapter: Chapter, status: str) -> None:
        """Commit both values through one durable journal before replacing either file."""
        with self.state_lock():
            self._recover_chapter_publication()
            self._state_local.__dict__.pop("publication", None)
            ChapterPublication.commit(
                self._publication_path,
                self.chapter_path(chapter.index),
                self.manifest_path,
                chapter,
                status,
            )

    def load_chapter(self, ci: int) -> Chapter:
        """Read and validate chapter state."""
        return Chapter.model_validate(self._read_formal_state(ci))

    @property
    def _publication_path(self) -> str:
        return os.path.join(self.run_dir, JOURNAL_NAME)

    def _read_chapter_publication(self) -> ChapterPublication | None:
        """Validate once per lock scope, including all chapter reads in one export snapshot."""
        if not hasattr(self._state_local, "publication"):
            self._state_local.publication = ChapterPublication.read(
                self._publication_path, self.manifest_path, self.chapter_path
            )
        return self._state_local.publication

    def _recover_chapter_publication(self) -> None:
        with self.state_lock():
            publication = self._read_chapter_publication()
            if publication is not None:
                # Drop the cached image even if a replacement fails: on re-entry its
                # old/new checks must use the latest materialized files.
                self._state_local.__dict__.pop("publication", None)
                publication.materialize(
                    self._publication_path, self.chapter_path(publication.index), self.manifest_path
                )

    def _read_formal_state(self, index: int | None = None) -> dict:
        with self._state_lock(create=False):
            publication = self._read_chapter_publication()
            if index is None:
                return (
                    deepcopy(publication.manifest.value)
                    if publication is not None
                    else self._read_json(self.manifest_path)
                )
            if publication is not None and index == publication.index:
                return deepcopy(publication.chapter.value)
            return self._read_json(self.chapter_path(index))

    @staticmethod
    def _formal_artifact(key: str) -> tuple[bool, int | None]:
        """Recognize the legacy names used to address formal state as JSON artifacts."""
        if os.name == "nt":
            key = key.lower()
        if key == "manifest.json":
            return True, None
        chapter = re.fullmatch(r"chapters/ch(0|[1-9][0-9]*)\.json", key)
        return (True, int(chapter[1])) if chapter else (False, None)

    def read_artifact(self, key: str) -> Any | None:
        self._artifact_key(key)
        if os.path.normcase(key) == os.path.normcase(JOURNAL_NAME):
            return None
        formal, index = self._formal_artifact(key)
        if formal:
            try:
                return self._read_formal_state(index)
            except FileNotFoundError:
                return None
        return super().read_artifact(key)

    def list_artifacts(self, prefix: str = "") -> list[str]:
        return [
            key
            for key in super().list_artifacts(prefix)
            if key != JOURNAL_NAME and not key.startswith(f"{JOURNAL_NAME}.")
        ]

    def read_artifact_records(self, key: str) -> list[dict]:
        # Formal state is a JSON document, not a JSONL stream.
        if self._formal_artifact(key)[0] or os.path.normcase(key) == os.path.normcase(JOURNAL_NAME):
            return []
        return super().read_artifact_records(key)

    def _require_mutable_artifact(self, key: str) -> None:
        self._artifact_key(key)
        if self._formal_artifact(key)[0] or os.path.normcase(key) == os.path.normcase(JOURNAL_NAME):
            raise ValueError(f"{key} is reserved for chapter and manifest publication")

    def write_artifact(self, key: str, value: Any) -> None:
        self._require_mutable_artifact(key)
        super().write_artifact(key, value)

    def delete_artifact(self, key: str) -> None:
        self._require_mutable_artifact(key)
        super().delete_artifact(key)

    def append_artifact_record(self, key: str, record: dict) -> None:
        self._require_mutable_artifact(key)
        super().append_artifact_record(key, record)

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

    @classmethod
    def from_parsed_document(
        cls, run_dir: str, document: Document, digest: str
    ) -> ExportSnapshotStore:
        """Build a read-only source edition without staging or committing translation state."""
        meta = dict(document.meta)
        meta.pop("epub_annotation_contexts", None)
        manifest = cls._document_manifest(document, digest, meta)
        chapters = {ch.index: ch for ch in document.chapters}
        if len(chapters) != len(document.chapters):
            raise ValueError("Duplicate chapter index in parsed document")
        return cls(run_dir, manifest, chapters)

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

    def _read_formal_state(self, index: int | None = None) -> dict:
        # Artifact access to formal state is frozen just like the typed readers.
        return self.load_manifest() if index is None else self.load_chapter(index).model_dump()
