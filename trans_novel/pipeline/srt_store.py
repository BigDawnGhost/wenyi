"""字幕翻译的轻量状态目录（不含术语库）。"""

from __future__ import annotations

import json
import os
from typing import Any

from .runstore import slugify, source_sha256


class SrtRunStore:
    """``state/srt/<slug>/``：manifest + 批次缓存 + 已提交译文。"""

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
    def translations_path(self) -> str:
        return os.path.join(self.run_dir, "translations.json")

    def ensure_manifest(self, source_path: str, *, cue_count: int) -> dict[str, Any]:
        """首次写入或校验同源后返回 manifest。"""
        digest = source_sha256(source_path)
        if os.path.isfile(self.manifest_path):
            manifest = self._read_json(self.manifest_path)
            if manifest.get("source_sha256") != digest:
                raise ValueError("字幕源文件与现有状态不一致；请更换 state 目录或删除旧状态后重跑")
            return manifest
        manifest = {
            "fmt": "srt",
            "source_path": os.path.abspath(source_path),
            "source_sha256": digest,
            "cue_count": cue_count,
            "batch_size": 20,
            "overlap_size": 10,
            "max_concurrent": 100,
        }
        self._write_json(self.manifest_path, manifest)
        return manifest

    def load_translations(self) -> dict[str, str]:
        """读取已提交的字幕译文映射。"""
        if not os.path.isfile(self.translations_path):
            return {}
        data = self._read_json(self.translations_path)
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}

    def save_translations(self, translations: dict[str, str]) -> None:
        """原子写入译文映射。"""
        self._write_json(self.translations_path, translations)

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
