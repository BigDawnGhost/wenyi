"""``FileStorage`` —— 基于本地文件系统的 Storage 实现（CLI 本地模式）。

组合现有 :class:`wenyi_core.pipeline.runstore.RunStore`（运行态：JSON 文件）
与 :class:`wenyi_core.glossary.store.GlossaryStore`（SQLite 术语库），
逐方法委托，行为与升级前完全一致。CLI 与现有测试均走此实现。
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.models import Chapter, Document
from ..pipeline.runstore import STATUS_DONE, RunStore  # noqa: F401 (re-export)
from .protocol import STATUS_PENDING  # noqa: F401


class FileStorage:
    """文件后端 Storage：一个对象同时承担 RunStore + GlossaryStore 职责。"""

    def __init__(self, run_dir: str, *, create: bool = True):
        self._run = RunStore(run_dir, create=create)
        self._glossary: Optional[GlossaryStore] = None

    # 幂等获取术语库连接（懒打开，复用同一连接）
    @property
    def _g(self) -> GlossaryStore:
        if self._glossary is None:
            self._glossary = GlossaryStore(self._run.glossary_path)
        return self._glossary

    # ── 路径属性（assemble / cli 需要）────────────────────────────────────
    @property
    def run_dir(self) -> str:
        return self._run.run_dir

    @property
    def source_dir(self) -> str:
        return self._run.source_dir

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

    # ── 生命周期 ─────────────────────────────────────────────────────────
    def exists(self) -> bool:
        return self._run.exists()

    def stage_document(self, doc: Document) -> dict:
        """写入章节文件并返回 manifest，但不提前落盘 manifest。"""
        return self._run.stage_document(doc)

    def init_from_document(self, doc: Document) -> dict:
        """兼容入口：stage + 立即保存 manifest（原子初始化完成标志）。"""
        manifest = self._run.stage_document(doc)
        manifest["initialized"] = True
        self._run.save_manifest(manifest)
        return manifest

    @contextmanager
    def lock(self) -> Iterator[None]:
        """书级文件锁（委托 RunStore.lock）。"""
        with self._run.lock():
            yield

    def close(self) -> None:
        if self._glossary is not None:
            self._glossary.close()
            self._glossary = None

    # ── 批次术语检查点（断点续跑）────────────────────────────────────────
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

    # ── 章节 / 段落 ──────────────────────────────────────────────────────
    def save_chapter(self, chapter: Chapter) -> None:
        self._run.save_chapter(chapter)

    def load_chapter(self, ci: int) -> Chapter:
        return self._run.load_chapter(ci)

    # ── 上下文 / 分析 / 报告 / usage ─────────────────────────────────────
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

    def save_usage(self, data: dict) -> None:
        self._run.save_usage(data)

    def load_usage(self) -> Optional[dict]:
        return self._run.load_usage()

    # ── 事件日志 ─────────────────────────────────────────────────────────
    def log_event(self, event: str, **data: Any) -> None:
        self._run.log_event(event, **data)

    def list_events(self, *, event_type: Optional[str] = None,
                    limit: int = 200) -> list[dict]:
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

    # ── 术语表（委托 GlossaryStore）──────────────────────────────────────
    def get_term(self, source: str) -> Optional[GlossaryTerm]:
        return self._g.get_term(source)

    def upsert_term(self, term: GlossaryTerm,
                    chapter: Optional[int] = None) -> str:
        return self._g.upsert_term(term, chapter=chapter)

    def all_terms(self) -> list[GlossaryTerm]:
        return self._g.all_terms()

    def terms_in(self, terms: list[GlossaryTerm],
                 text: str) -> list[GlossaryTerm]:
        return GlossaryStore.terms_in(terms, text)

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        return self._g.terms_in_text(text)

    def lock_term(self, source: str, target: Optional[str] = None) -> None:
        self._g.lock_term(source, target)

    def delete_term(self, source: str) -> bool:
        return self._g.delete_term(source)

    def mark_conflicts_resolved(self, source: str) -> None:
        self._g.mark_conflicts_resolved(source)

    def open_conflicts(self) -> list[dict]:
        return self._g.open_conflicts()

    def low_confidence_terms(self) -> list[GlossaryTerm]:
        return self._g.low_confidence_terms()

    # ── 翻译记忆库 ───────────────────────────────────────────────────────
    def add_tm(self, source_text: str, target_text: str,
               chapter: Optional[int] = None) -> None:
        self._g.add_tm(source_text, target_text, chapter)

    def tm_lookup(self, source_text: str) -> Optional[str]:
        return self._g.tm_lookup(source_text)

    # ── 统计 ─────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        return self._g.stats()
