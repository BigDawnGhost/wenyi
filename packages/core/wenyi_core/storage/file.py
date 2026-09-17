"""Local filesystem implementation of Storage for CLI workflows.

Compose :class:`wenyi_core.pipeline.runstore.RunStore` for JSON run state with
:class:`wenyi_core.glossary.store.GlossaryStore` for the SQLite glossary.
Domain services use the same storage interface as the Web backend.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.models import Chapter, Document
from ..pipeline.runstore import STATUS_DONE, RunStore  # noqa: F401 (re-export)
from .artifacts import FileArtifacts
from .protocol import STATUS_PENDING  # noqa: F401


class FileStorage(FileArtifacts):
    """Expose run state and glossary operations through a single file backend."""

    def __init__(self, run_dir: str, *, create: bool = True):
        FileArtifacts.__init__(self, run_dir)
        self._run = RunStore(run_dir, create=create)
        self._glossary: Optional[GlossaryStore] = None

    def __getattr__(self, name: str):
        # File-only state operations stay in RunStore; domain services receive this port.
        return getattr(self._run, name)

    @property
    def _batch_glossary_event_cache(self):
        return self._run._batch_glossary_event_cache

    @_batch_glossary_event_cache.setter
    def _batch_glossary_event_cache(self, value):
        self._run._batch_glossary_event_cache = value

    def state_lock(self):
        return self._run.state_lock()

    def assemble_lock(self):
        return self._run.assemble_lock()

    def finish_initialization(self) -> None:
        self._run.finish_initialization()

    def ensure_source_identity(self, input_path: str, *, actual_sha256: str | None = None) -> str:
        return self._run.ensure_source_identity(input_path, actual_sha256=actual_sha256)

    def create_export_snapshot(self, *, actual_sha256: str):
        return self._run.create_export_snapshot(actual_sha256=actual_sha256)

    def save_chapter_with_status(self, chapter: Chapter, status: str) -> None:
        self._run.save_chapter_with_status(chapter, status)

    def save_annotation_contexts(self, data: dict) -> None:
        self._run.save_annotation_contexts(data)

    def load_annotation_contexts(self) -> dict | None:
        return self._run.load_annotation_contexts()

    def prepare_usage_commit(self, ledgers: dict[str, dict]) -> None:
        self._run.prepare_usage_commit(ledgers)

    def recover_usage(self) -> None:
        self._run.recover_usage()

    def record_timing(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._run.record_timing(record)

    # Open the glossary lazily and reuse the connection within its lifetime.
    @property
    def _g(self) -> GlossaryStore:
        if self._glossary is None:
            self._glossary = GlossaryStore(self._run.glossary_path)
        return self._glossary

    # Paths used by assembly and CLI commands.
    @property
    def run_dir(self) -> str:
        return self._run.run_dir

    @property
    def source_dir(self) -> str:
        return self._run.source_dir

    @property
    def reviews_dir(self) -> str:
        return self._run.reviews_dir

    def chapter_path(self, ci: int) -> str:
        return self._run.chapter_path(ci)

    @property
    def glossary_path(self) -> str:
        return self._run.glossary_path

    @property
    def report_path(self) -> str:
        return self._run.report_path

    @property
    def manifest_path(self) -> str:
        return self._run.manifest_path

    @property
    def event_log_path(self) -> str:
        return self._run.event_log_path

    @property
    def usage_path(self) -> str:
        return self._run.usage_path

    @property
    def initialization_path(self) -> str:
        return self._run.initialization_path

    # Storage lifecycle.
    def begin_initialization(self, source_hash: str) -> None:
        self.close()
        self._run.begin_initialization(source_hash)

    def exists(self) -> bool:
        return self._run.exists()

    def stage_document(self, doc: Document, *, source_hash: str | None = None) -> dict:
        """Write chapter files and return the manifest without committing it."""
        return self._run.stage_document(doc, source_hash=source_hash)

    def init_from_document(self, doc: Document) -> dict:
        """Stage a document and commit the manifest as the initialization marker."""
        manifest = self._run.stage_document(doc)
        manifest["initialized"] = True
        self._run.save_manifest(manifest)
        return manifest

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold the book lock and release owned glossary connections before unlocking."""
        with self._run.lock():
            try:
                yield
            finally:
                self.close()

    def close(self) -> None:
        if self._glossary is not None:
            self._glossary.close()
            self._glossary = None

    # Batch glossary checkpoints for resumable runs.
    @staticmethod
    def batch_glossary_key(start_index: int, count: int) -> str:
        return RunStore.batch_glossary_key(start_index, count)

    def completed_batch_glossary_keys(self, chapter: int) -> set[str]:
        return self._run.completed_batch_glossary_keys(chapter)

    # ── manifest ─────────────────────────────────────────────────────────
    def save_manifest(self, manifest: dict) -> None:
        self._run.save_manifest(manifest)

    def load_manifest(self) -> dict:
        return self._run.load_manifest()

    def set_chapter_status(self, ci: int, status: str) -> None:
        self._run.set_chapter_status(ci, status)

    def pending_chapters(self) -> list[int]:
        return self._run.pending_chapters()

    # Chapters and segments.
    def save_chapter(self, chapter: Chapter) -> None:
        self._run.save_chapter(chapter)

    def load_chapter(self, ci: int) -> Chapter:
        return self._run.load_chapter(ci)

    # Context, analysis, reports and usage.
    def save_context(self, data: dict) -> None:
        self._run.save_context(data)

    def load_context(self) -> Optional[dict]:
        return self._run.load_context()

    def save_analysis(self, data: dict) -> None:
        self._run.save_analysis(data)

    def load_analysis(self) -> Optional[dict]:
        return self._run.load_analysis()

    def save_report(self, data: dict) -> None:
        self._run.save_report(data)

    def load_report(self) -> Optional[dict]:
        path = self._run.report_path
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_latest_review_result(self) -> Optional[dict]:
        return self._run.load_latest_review_result()

    def save_usage(self, data: dict) -> None:
        self._run.save_usage(data)

    def load_usage(self) -> Optional[dict]:
        return self._run.load_usage()

    # Event logs.
    def log_event(self, event: str, **data: Any) -> None:
        self._run.log_event(event, **data)

    def list_events(self, *, event_type: Optional[str] = None, limit: int = 200) -> list[dict]:
        path = self._run.event_log_path
        if not os.path.isfile(path):
            return []
        rows: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type and row.get("event") != event_type:
                    continue
                rows.append(row)
        return rows[-limit:] if limit else rows

    # Glossary operations delegated to GlossaryStore.
    def get_term(self, source: str) -> Optional[GlossaryTerm]:
        return self._g.get_term(source)

    def upsert_term(self, term: GlossaryTerm, chapter: Optional[int] = None) -> str:
        return self._g.upsert_term(term, chapter=chapter)

    def all_terms(self) -> list[GlossaryTerm]:
        if self._glossary is not None:
            return self._glossary.all_terms()
        if not os.path.isfile(self.glossary_path):
            return []
        return GlossaryStore.load_terms_readonly(self.glossary_path)

    def terms_in(self, terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        return GlossaryStore.terms_in(terms, text)

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        return self._g.terms_in(self._g.all_terms(), text)

    def resolve_term(self, source: str, target: str) -> bool:
        return self._g.resolve_term(source, target)

    def delete_term(self, source: str) -> bool:
        cursor = self._g.conn.execute("DELETE FROM glossary WHERE source=?", (source,))
        self._g.conn.commit()
        return cursor.rowcount > 0

    def mark_conflicts_resolved(self, source: str) -> None:
        self._g.mark_conflicts_resolved(source)

    def open_conflicts(self) -> list[dict]:
        return self._g.open_conflicts()

    def stats(self) -> dict[str, int]:
        return self._g.stats()
