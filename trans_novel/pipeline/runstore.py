"""运行态持久化：支持断点续跑。

目录结构（state_dir/<book-slug>/）：
  manifest.json     书籍元信息 + 各章状态
  chapters/ch{n}.json  各章（含 source/target 的 Segment）
  context.json      滚动上下文（梗概 + 前文尾段）
  analysis.json     全局分析结果
  usage.json        本书跨 translate/resume 累计的 LLM token 用量
  glossary.db       术语库 + 翻译记忆库
  report.json       QA 报告
  events.jsonl      追加式行为 / 改写 / 翻译结果日志
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

from ..ingest.models import Chapter, Document

STATUS_PENDING = "pending"
STATUS_DONE = "done"


def slugify(name: str) -> str:
    s = re.sub(r"[^\w一-鿿぀-ヿ-]+", "_", name).strip("_")
    return s or "book"


class RunStore:
    def __init__(self, run_dir: str, *, create: bool = True):
        self.run_dir = run_dir
        self.chapters_dir = os.path.join(run_dir, "chapters")
        self._batch_glossary_event_cache: dict[int, set[str]] | None = None
        if create:
            self.ensure_dirs()

    def ensure_dirs(self) -> None:
        os.makedirs(self.chapters_dir, exist_ok=True)

    # ── 路径 ──────────────────────────────────────────────────────────────
    @property
    def manifest_path(self) -> str:
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def context_path(self) -> str:
        return os.path.join(self.run_dir, "context.json")

    @property
    def analysis_path(self) -> str:
        return os.path.join(self.run_dir, "analysis.json")

    @property
    def glossary_path(self) -> str:
        return os.path.join(self.run_dir, "glossary.db")

    @property
    def report_path(self) -> str:
        return os.path.join(self.run_dir, "report.json")

    @property
    def usage_path(self) -> str:
        return os.path.join(self.run_dir, "usage.json")

    @property
    def event_log_path(self) -> str:
        return os.path.join(self.run_dir, "events.jsonl")

    def chapter_path(self, ci: int) -> str:
        return os.path.join(self.chapters_dir, f"ch{ci}.json")

    # ── 通用 JSON ─────────────────────────────────────────────────────────
    @staticmethod
    def _write_json(path: str, data) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，防写一半中断

    @staticmethod
    def _read_json(path: str):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def exists(self) -> bool:
        return os.path.isfile(self.manifest_path)

    # ── manifest ──────────────────────────────────────────────────────────
    def init_from_document(self, doc: Document) -> dict:
        manifest = {
            "title": doc.title,
            "fmt": doc.fmt,
            "source_path": doc.source_path,
            "source_lang": doc.source_lang,
            "target_lang": doc.target_lang,
            "meta": doc.meta,
            "chapters": [
                {"index": c.index, "title": c.title,
                 "href": c.href, "status": STATUS_PENDING}
                for c in doc.chapters
            ],
        }
        self.save_manifest(manifest)
        for c in doc.chapters:
            self.save_chapter(c)
        return manifest

    def save_manifest(self, manifest: dict) -> None:
        self._write_json(self.manifest_path, manifest)

    def load_manifest(self) -> dict:
        return self._read_json(self.manifest_path)

    def set_chapter_status(self, ci: int, status: str) -> None:
        manifest = self.load_manifest()
        for c in manifest["chapters"]:
            if c["index"] == ci:
                c["status"] = status
                break
        self.save_manifest(manifest)

    def pending_chapters(self) -> list[int]:
        manifest = self.load_manifest()
        return [c["index"] for c in manifest["chapters"] if c["status"] != STATUS_DONE]

    # ── 章 ────────────────────────────────────────────────────────────────
    def save_chapter(self, chapter: Chapter) -> None:
        self._write_json(self.chapter_path(chapter.index), chapter.to_dict())

    def load_chapter(self, ci: int) -> Chapter:
        return Chapter.from_dict(self._read_json(self.chapter_path(ci)))

    # ── 上下文 / 分析 / 报告 ──────────────────────────────────────────────
    def save_context(self, data: dict) -> None:
        self._write_json(self.context_path, data)

    def load_context(self) -> dict | None:
        return self._read_json(self.context_path) if os.path.isfile(self.context_path) else None

    def save_analysis(self, data: dict) -> None:
        self._write_json(self.analysis_path, data)

    def load_analysis(self) -> dict | None:
        return self._read_json(self.analysis_path) if os.path.isfile(self.analysis_path) else None

    def save_report(self, data: dict) -> None:
        self._write_json(self.report_path, data)

    def save_usage(self, data: dict) -> None:
        self._write_json(self.usage_path, data)

    def load_usage(self) -> dict | None:
        return self._read_json(self.usage_path) if os.path.isfile(self.usage_path) else None

    # ── 批次恢复检查点 ────────────────────────────────────────────────────
    @staticmethod
    def batch_glossary_key(start_index: int, count: int) -> str:
        """返回批次术语抽取检查点键；批次边界变化时不会误命中旧键。"""
        return f"{start_index}:{count}"

    def completed_batch_glossary_keys(self, chapter: int) -> set[str]:
        """从事件日志恢复已完成的批次术语抽取；每个实例最多扫描一次。"""
        if self._batch_glossary_event_cache is None:
            completed: dict[int, set[str]] = {}
            if os.path.isfile(self.event_log_path):
                with open(self.event_log_path, "r", encoding="utf-8") as f:
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
                        completed.setdefault(ci, set()).add(
                            self.batch_glossary_key(start, count)
                        )
            self._batch_glossary_event_cache = completed
        return set(self._batch_glossary_event_cache.get(chapter, set()))

    # ── 追加式事件日志 ────────────────────────────────────────────────────
    def log_event(self, event: str, **data: Any) -> None:
        """追加一条 JSONL 事件，用于翻译行为、改写前后和产物对账。"""
        self.ensure_dirs()
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        with open(self.event_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        if event == "batch_glossary_extracted" and self._batch_glossary_event_cache is not None:
            chapter = data.get("chapter")
            start = data.get("start_index")
            count = data.get("count")
            if (
                isinstance(chapter, int)
                and isinstance(start, int)
                and isinstance(count, int)
            ):
                self._batch_glossary_event_cache.setdefault(chapter, set()).add(
                    self.batch_glossary_key(start, count)
                )
