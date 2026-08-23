"""运行态持久化：支持断点续跑。

目录结构（state_dir/<book-slug>/）：
  manifest.json     书籍元信息 + 各章状态
  chapters/ch{n}.json  各章（含 source/target 的 Segment）
  source/           输入预处理缓存（例如 PDF 转换后的 HTML）
  annotation_contexts.json  EPUB 注释目标的去重源文索引
  context.json      滚动上下文（梗概 + 前文尾段）
  analysis.json     全局分析结果
  usage.json        本书跨 translate/resume 累计的 LLM token 用量
  run_metrics/      每次顶层运行独立的耗时、用量和版本账本
  glossary.db       术语库 + 译法冲突记录
  report.json       翻译报告
  events.jsonl      追加式行为 / 改写 / 翻译结果日志
  reviews/          每次只读全书 Review 的正式结果与逐轮记录
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from typing import Any

from ..ingest.models import Chapter, Document

STATUS_PENDING = "pending"
STATUS_DONE = "done"


def slugify(name: str) -> str:
    """把书名转换为适合作为状态目录名的稳定短名。"""
    s = re.sub(r"[^\w一-鿿぀-ヿ-]+", "_", name).strip("_")
    return s or "book"


def source_sha256(path: str) -> str:
    """流式计算源文件 SHA-256，避免把整本书一次性读入内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class RunStore:
    def __init__(self, run_dir: str, *, create: bool = True):
        """绑定一本书的状态目录，并按需创建章节子目录。"""
        self.run_dir = run_dir
        self.chapters_dir = os.path.join(run_dir, "chapters")
        self._batch_glossary_event_cache: dict[int, set[str]] | None = None
        if create:
            self.ensure_dirs()

    def ensure_dirs(self) -> None:
        """创建运行目录及章节状态子目录。"""
        os.makedirs(self.chapters_dir, exist_ok=True)

    @contextmanager
    def _file_lock(self, filename: str) -> Iterator[None]:
        """用状态目录内的指定锁文件串行化跨进程操作。"""
        self.ensure_dirs()
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
    def lock(self) -> Iterator[None]:
        """串行化同一本书的翻译、审校和报告等长时间运行。"""
        with self._file_lock(".run.lock"):
            yield

    @contextmanager
    def state_lock(self) -> Iterator[None]:
        """短暂冻结 manifest 与章节文件，供原子落盘或一致快照使用。"""
        with self._file_lock(".state.lock"):
            yield

    @contextmanager
    def event_lock(self) -> Iterator[None]:
        """串行化 JSONL 事件追加，避免并发命令交错写入同一行。"""
        with self._file_lock(".events.lock"):
            yield

    @contextmanager
    def assemble_lock(self) -> Iterator[None]:
        """串行化同一本书的产物写入，但不阻塞仍在进行的正文翻译。"""
        with self._file_lock(".assemble.lock"):
            yield

    # ── 路径 ──────────────────────────────────────────────────────────────
    @property
    def manifest_path(self) -> str:
        """返回书籍清单文件路径。"""
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def initialization_path(self) -> str:
        """返回未完成初始化使用的临时源身份文件。"""
        return os.path.join(self.run_dir, ".initializing.json")

    @property
    def context_path(self) -> str:
        """返回滚动上下文文件路径。"""
        return os.path.join(self.run_dir, "context.json")

    @property
    def annotation_contexts_path(self) -> str:
        """返回 EPUB 注释辅助上下文索引路径。"""
        return os.path.join(self.run_dir, "annotation_contexts.json")

    @property
    def analysis_path(self) -> str:
        """返回全书风格分析文件路径。"""
        return os.path.join(self.run_dir, "analysis.json")

    @property
    def glossary_path(self) -> str:
        """返回术语及译法冲突数据库路径。"""
        return os.path.join(self.run_dir, "glossary.db")

    @property
    def report_path(self) -> str:
        """返回质量报告文件路径。"""
        return os.path.join(self.run_dir, "report.json")

    @property
    def usage_path(self) -> str:
        """返回本书累计 token 用量文件路径。"""
        return os.path.join(self.run_dir, "usage.json")

    @property
    def run_metrics_dir(self) -> str:
        """返回每次运行独立指标账本的目录。"""
        return os.path.join(self.run_dir, "run_metrics")

    @property
    def event_log_path(self) -> str:
        """返回追加式 JSONL 事件日志路径。"""
        return os.path.join(self.run_dir, "events.jsonl")

    def chapter_path(self, ci: int) -> str:
        """返回指定章节索引对应的状态文件路径。"""
        return os.path.join(self.chapters_dir, f"ch{ci}.json")

    @property
    def source_dir(self) -> str:
        """返回输入预处理缓存目录；由具体读取器按需创建。"""
        return os.path.join(self.run_dir, "source")

    @property
    def reviews_dir(self) -> str:
        """返回只读全书 Review 的运行记录目录。"""
        return os.path.join(self.run_dir, "reviews")

    # ── 通用 JSON ─────────────────────────────────────────────────────────
    @staticmethod
    def _write_json(path: str, data) -> None:
        """通过同目录临时文件原子写入格式化 JSON。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，防写一半中断

    @staticmethod
    def _read_json(path: str):
        """读取并解析 UTF-8 JSON 文件。"""
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def exists(self) -> bool:
        """判断运行状态是否已完成初始化并写入 manifest。"""
        return os.path.isfile(self.manifest_path)

    def begin_initialization(self, source_hash: str) -> None:
        """清理未完成初始化的派生状态，并记录本次源内容身份。

        PDF 转换缓存按哈希隔离且代价较高，因此保留 ``source/``；同一源文件
        的失败运行指标和事件也保留。章节、术语、分析等可变派生物一律重建，
        防止上次在 manifest 提交前失败时留下的数据污染新任务。
        """
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("源文件 SHA-256 格式无效")

        previous_hash: str | None = None
        if os.path.isfile(self.initialization_path):
            try:
                marker = self._read_json(self.initialization_path)
            except (OSError, json.JSONDecodeError, TypeError):
                marker = None
            if isinstance(marker, dict) and isinstance(marker.get("source_sha256"), str):
                previous_hash = marker["source_sha256"]

        shutil.rmtree(self.chapters_dir, ignore_errors=True)
        os.makedirs(self.chapters_dir, exist_ok=True)
        for path in (
            self.analysis_path,
            self.annotation_contexts_path,
            self.context_path,
            self.glossary_path,
            f"{self.glossary_path}-wal",
            f"{self.glossary_path}-shm",
            f"{self.glossary_path}-journal",
            self.report_path,
            self.usage_path,
            f"{self.manifest_path}.tmp",
        ):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

        if previous_hash != source_hash:
            shutil.rmtree(self.reviews_dir, ignore_errors=True)
            shutil.rmtree(self.run_metrics_dir, ignore_errors=True)
            try:
                os.remove(self.event_log_path)
            except FileNotFoundError:
                pass

        self._batch_glossary_event_cache = None
        self._write_json(
            self.initialization_path,
            {"source_sha256": source_hash},
        )

    def finish_initialization(self) -> None:
        """在 manifest 已成功提交后清除临时初始化身份。"""
        try:
            os.remove(self.initialization_path)
        except FileNotFoundError:
            pass

    # ── manifest ──────────────────────────────────────────────────────────
    def stage_document(
        self,
        doc: Document,
        *,
        source_hash: str | None = None,
    ) -> dict:
        """写入初始章节文件并返回 manifest 内容，但不提前写 manifest。

        manifest 是一次运行初始化完成的标志，由调用方在分析、术语库
        和上下文都已落盘后最后保存。
        """
        digest = source_hash or source_sha256(doc.source_path)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("源文件 SHA-256 格式无效")
        document_meta = dict(doc.meta)
        annotation_contexts = document_meta.pop("epub_annotation_contexts", None)
        if isinstance(annotation_contexts, dict) and annotation_contexts:
            self.save_annotation_contexts(annotation_contexts)
        else:
            try:
                os.remove(self.annotation_contexts_path)
            except FileNotFoundError:
                pass

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
        """校验输入内容属于当前状态；缺少内容身份的旧状态直接拒绝。"""
        actual = actual_sha256 or source_sha256(input_path)
        with self.state_lock():
            self._validate_source_identity(self.load_manifest(), actual)
        return actual

    @staticmethod
    def _validate_source_identity(manifest: dict, actual: str) -> None:
        """校验已读取 manifest 的源内容身份。"""
        if not re.fullmatch(r"[0-9a-f]{64}", actual):
            raise ValueError("源文件 SHA-256 格式无效")

        expected = manifest.get("source_sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(
                "现有翻译状态缺少有效的 source_sha256；请删除该状态目录并重新建立翻译状态。"
            )
        if expected != actual:
            raise ValueError(
                "输入文件内容与现有翻译状态不一致（状态目录同名）；请使用原始源文件，"
                "或移走该状态目录后重新建立。"
            )

    def create_export_snapshot(self, *, actual_sha256: str) -> ExportSnapshotStore:
        """在短状态锁内冻结导出所需的 manifest 和全部章节。"""
        with self.state_lock():
            manifest = self.load_manifest()
            self._validate_source_identity(manifest, actual_sha256)
            chapter_entries = manifest.get("chapters")
            if not isinstance(chapter_entries, list):
                raise ValueError("翻译状态中的章节清单格式无效")

            chapters: dict[int, Chapter] = {}
            for entry in chapter_entries:
                if not isinstance(entry, dict):
                    raise ValueError("翻译状态中的章节条目格式无效")
                chapter_index = entry.get("index")
                if not isinstance(chapter_index, int) or isinstance(chapter_index, bool):
                    raise ValueError("翻译状态中的章节编号无效")
                if chapter_index in chapters:
                    raise ValueError(f"翻译状态包含重复章节编号：{chapter_index}")
                chapter = self.load_chapter(chapter_index)
                if chapter.index != chapter_index:
                    raise ValueError(f"章节文件索引不匹配：{chapter_index}")
                chapters[chapter_index] = chapter

        return ExportSnapshotStore(self.run_dir, manifest, chapters)

    def save_manifest(self, manifest: dict) -> None:
        """原子保存书籍清单和章节状态。"""
        with self.state_lock():
            self._write_json(self.manifest_path, manifest)

    def load_manifest(self) -> dict:
        """读取书籍清单和章节状态。"""
        return self._read_json(self.manifest_path)

    def set_chapter_status(self, ci: int, status: str) -> None:
        """更新指定章节状态并原子保存整份 manifest。"""
        with self.state_lock():
            manifest = self.load_manifest()
            for c in manifest["chapters"]:
                if c["index"] == ci:
                    c["status"] = status
                    break
            self._write_json(self.manifest_path, manifest)

    def pending_chapters(self) -> list[int]:
        """返回尚未标记完成的章节索引。"""
        manifest = self.load_manifest()
        return [c["index"] for c in manifest["chapters"] if c["status"] != STATUS_DONE]

    # ── 章 ────────────────────────────────────────────────────────────────
    def save_chapter(self, chapter: Chapter) -> None:
        """原子保存一个章节的源文、译文和阶段元数据。"""
        with self.state_lock():
            self._write_json(self.chapter_path(chapter.index), chapter.to_dict())

    def save_chapter_with_status(self, chapter: Chapter, status: str) -> None:
        """在同一状态锁内发布最终章节内容及其 manifest 状态。"""
        with self.state_lock():
            self._write_json(self.chapter_path(chapter.index), chapter.to_dict())
            manifest = self.load_manifest()
            for entry in manifest["chapters"]:
                if entry["index"] == chapter.index:
                    entry["status"] = status
                    break
            self._write_json(self.manifest_path, manifest)

    def load_chapter(self, ci: int) -> Chapter:
        """读取并校验指定章节状态。"""
        return Chapter.from_dict(self._read_json(self.chapter_path(ci)))

    # ── 上下文 / 分析 / 报告 ──────────────────────────────────────────────
    def save_context(self, data: dict) -> None:
        """原子保存滚动上下文快照。"""
        self._write_json(self.context_path, data)

    def load_context(self) -> dict | None:
        """读取滚动上下文；文件尚不存在时返回 None。"""
        return self._read_json(self.context_path) if os.path.isfile(self.context_path) else None

    def save_annotation_contexts(self, data: dict) -> None:
        """原子保存 EPUB 注释目标的去重源文索引。"""
        self._write_json(self.annotation_contexts_path, data)

    def load_annotation_contexts(self) -> dict | None:
        """读取 EPUB 注释辅助上下文；非 EPUB 或尚无索引时返回 None。"""
        return (
            self._read_json(self.annotation_contexts_path)
            if os.path.isfile(self.annotation_contexts_path)
            else None
        )

    def save_analysis(self, data: dict) -> None:
        """原子保存全书分析和概览数据。"""
        self._write_json(self.analysis_path, data)

    def load_analysis(self) -> dict | None:
        """读取全书分析；文件尚不存在时返回 None。"""
        return self._read_json(self.analysis_path) if os.path.isfile(self.analysis_path) else None

    def save_report(self, data: dict) -> None:
        """原子保存质量检查报告。"""
        self._write_json(self.report_path, data)

    def save_usage(self, data: dict) -> None:
        """原子保存本书累计 token 用量。"""
        self._write_json(self.usage_path, data)

    def load_usage(self) -> dict | None:
        """读取累计 token 用量；文件尚不存在时返回 None。"""
        return self._read_json(self.usage_path) if os.path.isfile(self.usage_path) else None

    def load_latest_review_result(self) -> dict[str, Any] | None:
        """按目录时间顺序读取最近一次完整或失败的 Review 结果。"""
        if not os.path.isdir(self.reviews_dir):
            return None
        for name in sorted(os.listdir(self.reviews_dir), reverse=True):
            if not name.startswith("review-"):
                continue
            path = os.path.join(self.reviews_dir, name, "result.json")
            if not os.path.isfile(path):
                continue
            try:
                result = self._read_json(path)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if isinstance(result, dict):
                return result
        return None

    def save_run_metric(self, record: dict[str, Any]) -> str:
        """按 run_id 原子保存一次运行指标并返回文件路径。"""
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", run_id
        ):
            raise ValueError("run_id 只能包含字母、数字、点、下划线、加号和连字符")
        path = os.path.join(self.run_metrics_dir, f"{run_id}.json")
        self._write_json(path, record)
        return path

    def load_run_metrics(self) -> list[dict[str, Any]]:
        """按文件名顺序读取全部单次运行账本。"""
        if not os.path.isdir(self.run_metrics_dir):
            return []
        return [
            self._read_json(os.path.join(self.run_metrics_dir, name))
            for name in sorted(os.listdir(self.run_metrics_dir))
            if name.endswith(".json")
        ]

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
                        completed.setdefault(ci, set()).add(self.batch_glossary_key(start, count))
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
        with self.event_lock():
            with open(self.event_log_path, "a", encoding="utf-8") as f:
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
    """导出专用的内存只读状态；源资源仍引用正式状态目录。"""

    def __init__(
        self,
        run_dir: str,
        manifest: dict,
        chapters: dict[int, Chapter],
    ) -> None:
        super().__init__(run_dir, create=False)
        self._snapshot_manifest = deepcopy(manifest)
        self._snapshot_chapters = {
            index: Chapter.from_dict(chapter.to_dict()) for index, chapter in chapters.items()
        }

    def load_manifest(self) -> dict:
        """返回冻结 manifest 的独立副本。"""
        return deepcopy(self._snapshot_manifest)

    def load_chapter(self, ci: int) -> Chapter:
        """返回冻结章节的独立副本，防止一次渲染污染后续输出。"""
        try:
            chapter = self._snapshot_chapters[ci]
        except KeyError as error:
            raise FileNotFoundError(f"快照中不存在章节：{ci}") from error
        return Chapter.from_dict(chapter.to_dict())
