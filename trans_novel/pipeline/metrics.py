"""单次流水线运行的可复现实验账本。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

from .. import __version__
from ..config import Config
from ..llm.base import LLMClient
from ..llm.usage import usage_delta

if TYPE_CHECKING:
    from .runstore import RunStore


_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "access_token",
    "api_token",
    "auth_token",
    "bearer_token",
)


def _now_iso() -> str:
    """返回带本地时区、可排序的秒级时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_value(value: Any, *, key: str = "") -> Any:
    """把配置值转换为可序列化数据，并隐藏可能的凭据。"""
    normalized_key = key.lower().replace("-", "_")
    if normalized_key == "token" or any(
        part in normalized_key for part in _SENSITIVE_KEY_PARTS
    ):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(item_key): _safe_value(item_value, key=str(item_key))
            for item_key, item_value in sorted(
                value.items(), key=lambda item: str(item[0])
            )
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return f"<{type(value).__name__}>"


def _fingerprint(data: Any) -> str:
    """计算规范 JSON 的 SHA-256，用于比较输入和配置是否相同。"""
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def config_identity(config: Config) -> dict[str, Any]:
    """提取影响翻译结果的非敏感配置，并给出稳定指纹。"""
    summary = {
        "language": {
            "source": config.source_lang,
            "target": config.target_lang,
        },
        "llm": {
            "provider": config.llm.provider,
            "timeout": config.llm.timeout,
            "max_retries": config.llm.max_retries,
            "tiers": {
                name: {
                    "model": tier.model,
                    "options": _safe_value(tier.options),
                }
                for name, tier in sorted(config.llm.tiers.items())
            },
        },
        "segment": _safe_value(config.segment.model_dump(mode="python")),
        "pipeline": _safe_value(config.pipeline.model_dump(mode="python")),
        "output": _safe_value(config.output.model_dump(mode="python")),
        "honorific_strategy": config.honorific_strategy,
        "punctuation_normalize": config.punctuation_normalize,
    }
    return {"fingerprint": _fingerprint(summary), "summary": summary}


def input_identity(input_path: str) -> dict[str, Any]:
    """记录输入文件的名称、大小和内容指纹，不保存完整路径或正文。"""
    path = Path(input_path)
    identity: dict[str, Any] = {
        "name": path.name,
        "suffix": path.suffix.lower(),
        "exists": path.is_file(),
    }
    if not identity["exists"]:
        return identity

    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        identity["size_bytes"] = path.stat().st_size
        identity["sha256"] = digest.hexdigest()
    except OSError:
        identity["readable"] = False
    return identity


def _git_output(repo_root: Path, *args: str) -> str | None:
    """读取 Git 身份信息；非 Git 安装或超时时静默降级。"""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def code_identity() -> dict[str, Any]:
    """记录包版本和 Git 提交，令不同分支的实验可追溯。"""
    repo_root = Path(__file__).resolve().parents[2]
    revision = _git_output(repo_root, "rev-parse", "HEAD")
    branch = _git_output(repo_root, "branch", "--show-current")
    status = _git_output(repo_root, "status", "--porcelain")
    return {
        "package_version": __version__,
        "git_revision": revision,
        "git_branch": branch,
        "git_dirty": bool(status) if revision else None,
    }


def _state_summary(store: RunStore) -> dict[str, int]:
    """汇总结束时的章节和正文段完成度，不复制书籍内容。"""
    manifest = store.load_manifest()
    chapters = manifest.get("chapters", [])
    summary = {
        "chapters_total": len(chapters),
        "chapters_translated": sum(
            item.get("status") == "done" for item in chapters
        ),
        "chapters_reviewed": sum(
            item.get("review_status") == "done" for item in chapters
        ),
        "segments_total": 0,
        "segments_translated": 0,
    }
    for item in chapters:
        chapter = store.load_chapter(item["index"])
        text_segments = chapter.text_segments
        summary["segments_total"] += len(text_segments)
        summary["segments_translated"] += sum(
            bool(segment.target and segment.target.strip())
            for segment in text_segments
        )
    return summary


@dataclass
class RunMetricsRecorder:
    """收集一次顶层操作的耗时、用量和可复现身份。"""

    operation: str
    requested_steps: list[str]
    input: dict[str, Any]
    config: dict[str, Any]
    code: dict[str, Any]
    usage_before: dict[str, Any]
    run_id: str = field(default_factory=lambda: (
        f"run-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f%z')}-"
        f"{uuid.uuid4().hex[:8]}"
    ))
    started_at: str = field(default_factory=_now_iso)
    _started: float = field(default_factory=time.perf_counter)
    _store: RunStore | None = None
    _stage_seconds: dict[str, float] = field(default_factory=dict)
    _stage_started: dict[str, float] = field(default_factory=dict)
    _stage_depth: dict[str, int] = field(default_factory=dict)
    _finished: bool = False

    @classmethod
    def start(
        cls,
        *,
        operation: str,
        requested_steps: list[str],
        input_path: str,
        config: Config,
        client: LLMClient,
    ) -> RunMetricsRecorder:
        """在任何模型调用之前抓取本次运行的基线。"""
        return cls(
            operation=operation,
            requested_steps=list(requested_steps),
            input=input_identity(input_path),
            config=config_identity(config),
            code=code_identity(),
            usage_before=client.usage_summary(),
        )

    def attach_store(self, store: RunStore) -> None:
        """绑定状态目录；只接受本次操作实际使用的第一本书。"""
        if self._store is None:
            self._store = store
            return
        if self._store.run_dir != store.run_dir:
            raise ValueError("一次运行账本不能跨越多个书籍状态目录")

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """累加一个阶段的墙钟时间；同名嵌套只计算一次。"""
        depth = self._stage_depth.get(name, 0)
        self._stage_depth[name] = depth + 1
        if depth == 0:
            self._stage_started[name] = time.perf_counter()
        try:
            yield
        finally:
            remaining = self._stage_depth[name] - 1
            self._stage_depth[name] = remaining
            if remaining == 0:
                elapsed = time.perf_counter() - self._stage_started.pop(name)
                self._stage_seconds[name] = (
                    self._stage_seconds.get(name, 0.0) + elapsed
                )

    def finish(
        self,
        client: LLMClient,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> str | None:
        """完成并持久化账本；未能建立状态目录时不额外创建孤立记录。"""
        if self._finished:
            return None
        self._finished = True
        if self._store is None:
            return None

        record: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "operation": self.operation,
            "requested_steps": self.requested_steps,
            "status": status,
            "started_at": self.started_at,
            "finished_at": _now_iso(),
            "duration_seconds": round(time.perf_counter() - self._started, 6),
            "stage_seconds": {
                name: round(seconds, 6)
                for name, seconds in sorted(self._stage_seconds.items())
            },
            "input": self.input,
            "config": self.config,
            "code": self.code,
            "usage": usage_delta(client.usage_summary(), self.usage_before),
        }
        try:
            record["state"] = _state_summary(self._store)
        except (OSError, KeyError, TypeError, ValueError):
            record["state"] = {"available": False}
        if error is not None:
            record["error"] = {"type": type(error).__name__}
        return self._store.save_run_metric(record)
