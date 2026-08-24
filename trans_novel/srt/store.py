"""字幕翻译的轻量状态目录（不含术语库）。

目录结构（state/srt/<slug>/）：
  manifest.json   源身份 + 滑窗配置 + 进度
  cues.jsonl      每行一条字幕（含 target/status）
  batches/        滑窗批次原始模型结果（续跑跳过用）
  usage.json      跨 resume 累计 LLM token 用量
  events.jsonl    追加式行为 / LLM 重试日志
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from ..pipeline.runstore import slugify, source_sha256

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class SrtRunStore:
    """``state/srt/<slug>/``：manifest + cues + 批次缓存 + usage/events。"""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        self.batches_dir = os.path.join(run_dir, "batches")
        os.makedirs(self.batches_dir, exist_ok=True)

    @classmethod
    def for_source(cls, state_dir: str, source_path: str) -> "SrtRunStore":
        """按源文件名 slug 定位字幕状态目录。"""
        stem = os.path.splitext(os.path.basename(source_path))[0]
        run_dir = os.path.join(state_dir, "srt", slugify(stem))
        return cls(run_dir)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.run_dir, "manifest.json")

    @property
    def cues_path(self) -> str:
        return os.path.join(self.run_dir, "cues.jsonl")

    @property
    def usage_path(self) -> str:
        return os.path.join(self.run_dir, "usage.json")

    @property
    def event_log_path(self) -> str:
        return os.path.join(self.run_dir, "events.jsonl")

    @contextmanager
    def _file_lock(self, filename: str) -> Iterator[None]:
        """用状态目录内的指定锁文件串行化跨进程操作。"""
        os.makedirs(self.run_dir, exist_ok=True)
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
    def event_lock(self) -> Iterator[None]:
        """串行化 JSONL 事件追加，避免并发交错写入同一行。"""
        with self._file_lock(".events.lock"):
            yield

    def ensure_manifest(
        self,
        source_path: str,
        *,
        cue_count: int,
        title: str | None = None,
        source_lang: str = "auto",
        target_lang: str = "zh",
        batch_size: int = 20,
        overlap_size: int = 10,
        max_concurrent: int = 100,
    ) -> dict[str, Any]:
        """首次写入或校验同源后返回 manifest。"""
        digest = source_sha256(source_path)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if os.path.isfile(self.manifest_path):
            manifest = self._read_json(self.manifest_path)
            if manifest.get("source_sha256") != digest:
                raise ValueError("字幕源文件与现有状态不一致；请更换 state 目录或删除旧状态后重跑")
            return manifest
        stem = os.path.splitext(os.path.basename(source_path))[0]
        manifest = {
            "fmt": "srt",
            "title": title or stem,
            "source_path": os.path.abspath(source_path),
            "source_sha256": digest,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "cue_count": cue_count,
            "done_count": 0,
            "status": "running",
            "batch_size": batch_size,
            "overlap_size": overlap_size,
            "max_concurrent": max_concurrent,
            "created_at": now,
            "updated_at": now,
        }
        self._write_json(self.manifest_path, manifest)
        return manifest

    def update_manifest(self, **fields: Any) -> dict[str, Any]:
        """合并更新 manifest 字段并刷新 updated_at。"""
        manifest = self._read_json(self.manifest_path) if os.path.isfile(self.manifest_path) else {}
        manifest.update(fields)
        manifest["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._write_json(self.manifest_path, manifest)
        return manifest

    def load_cues(self) -> dict[str, dict[str, Any]]:
        """读取 cues.jsonl，按 index 建表；文件不存在时返回空表。"""
        if not os.path.isfile(self.cues_path):
            return {}
        cues: dict[str, dict[str, Any]] = {}
        with open(self.cues_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                index = row.get("index")
                if index is None:
                    continue
                cues[str(index)] = row
        return cues

    def save_cues(self, cues: dict[str, dict[str, Any]]) -> None:
        """按 index 数值序原子重写整个 cues.jsonl。"""

        def sort_key(index: str) -> tuple[int, int, str]:
            try:
                return (0, int(index), index)
            except ValueError:
                return (1, 0, index)

        os.makedirs(self.run_dir, exist_ok=True)
        tmp = f"{self.cues_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for index in sorted(cues.keys(), key=sort_key):
                handle.write(json.dumps(cues[index], ensure_ascii=False) + "\n")
        os.replace(tmp, self.cues_path)

    def ensure_cues(
        self,
        source_cues: list[tuple[str, str, str]],
    ) -> dict[str, dict[str, Any]]:
        """用源字幕初始化或补齐 cues；保留已有 target/status。

        ``source_cues`` 每项为 ``(index, timestamp, source_text)``。
        """
        existing = self.load_cues()
        merged: dict[str, dict[str, Any]] = {}
        for index, timestamp, source in source_cues:
            key = str(index)
            prev = existing.get(key)
            if prev is not None:
                row = dict(prev)
                row["timestamp"] = timestamp
                row["source"] = source
                if not row.get("target"):
                    row["status"] = STATUS_PENDING
                merged[key] = row
            else:
                merged[key] = {
                    "index": key,
                    "timestamp": timestamp,
                    "source": source,
                    "target": "",
                    "status": STATUS_PENDING,
                }
        self.save_cues(merged)
        return merged

    def translations_from_cues(self, cues: dict[str, dict[str, Any]]) -> dict[str, str]:
        """从 cues 提取已完成译文映射。"""
        out: dict[str, str] = {}
        for index, row in cues.items():
            target = row.get("target")
            status = row.get("status")
            if isinstance(target, str) and target and status == STATUS_DONE:
                out[str(index)] = target
            elif isinstance(target, str) and target:
                out[str(index)] = target
        return out

    def apply_translations(
        self,
        cues: dict[str, dict[str, Any]],
        translations: dict[str, str],
        *,
        status: str = STATUS_DONE,
    ) -> dict[str, dict[str, Any]]:
        """把译文写回 cues 内存表（不落盘）。"""
        for index, target in translations.items():
            key = str(index)
            row = cues.get(key)
            if row is None:
                continue
            row["target"] = target
            row["status"] = status
        return cues

    def batch_path(self, batch_start: int) -> str:
        return os.path.join(self.batches_dir, f"{batch_start:06d}.json")

    def load_batch(self, batch_start: int) -> dict[str, str] | None:
        path = self.batch_path(batch_start)
        if not os.path.isfile(path):
            return None
        data = self._read_json(path)
        raw = data.get("translations")
        if not isinstance(raw, dict):
            return None
        return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}

    def save_batch(self, batch_start: int, translations: dict[str, str]) -> None:
        self._write_json(
            self.batch_path(batch_start),
            {"batch_start": batch_start, "translations": translations},
        )

    def save_usage(self, data: dict[str, Any]) -> None:
        """原子保存累计 token 用量。"""
        self._write_json(self.usage_path, data)

    def load_usage(self) -> dict[str, Any] | None:
        """读取累计 token 用量；文件尚不存在时返回 None。"""
        if not os.path.isfile(self.usage_path):
            return None
        return self._read_json(self.usage_path)

    def log_event(self, event: str, **data: Any) -> None:
        """追加一条 JSONL 事件。"""
        os.makedirs(self.run_dir, exist_ok=True)
        row = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **data,
        }
        with self.event_lock():
            with open(self.event_log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _read_json(path: str) -> dict[str, Any]:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _write_json(path: str, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
