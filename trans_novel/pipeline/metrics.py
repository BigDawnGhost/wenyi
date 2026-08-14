"""单次流水线运行的可复现实验账本。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import Config
from ..llm.base import LLMClient
from ..llm.usage import usage_delta
from .runstore import source_sha256

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


def _package_version() -> str:
    """读取已安装包版本；源码树未安装时稳定降级。"""
    try:
        return version("trans-novel")
    except PackageNotFoundError:
        return "unknown"


def _now_iso() -> str:
    """返回带本地时区、可排序的秒级时间。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_value(value: Any, *, key: str = "") -> Any:
    """把配置值转换为可序列化数据，并隐藏可能的凭据。"""
    if _is_sensitive_key(key):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {
            str(item_key): _safe_value(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    return f"<{type(value).__name__}>"


def _is_sensitive_key(key: str) -> bool:
    """判断配置键是否可能承载凭据。"""
    normalized = key.lower().replace("-", "_")
    return normalized == "token" or any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _fingerprint(data: Any) -> str:
    """计算规范 JSON 的 SHA-256，用于比较输入和配置是否相同。"""
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_base_url(
    value: str | None,
    *,
    include_path: bool = False,
) -> str | None:
    """保留端点身份所需部分，同时去掉 URL 中可能携带的凭据和查询参数。"""
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        path = parsed.path if include_path else ""
        query = ""
        if include_path:
            safe_query = sorted(
                (key, item_value)
                for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
                if not _is_sensitive_key(key)
            )
            query = urlencode(safe_query)
        return urlunsplit((parsed.scheme, hostname, path, query, ""))
    except ValueError:
        # 非法端口等畸形 URL 也不能原样写入账本。
        return "<invalid-url>"


def config_identity(config: Config) -> dict[str, Any]:
    """提取影响翻译结果的非敏感配置，并给出稳定指纹。"""
    base_url = config.llm.base_url
    endpoint_identity = _safe_base_url(base_url, include_path=True)
    summary = {
        "language": {
            "source": config.source_lang,
            "target": config.target_lang,
        },
        "llm": {
            "provider": config.llm.provider,
            "base_url": _safe_base_url(base_url),
            "base_url_fingerprint": (
                _fingerprint(endpoint_identity) if endpoint_identity else None
            ),
            "reasoning_style": config.llm.reasoning_style,
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
    identity, _signature = _capture_input_identity(input_path)
    return identity


def _source_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    """返回用于发现普通文件替换/改写的廉价稳定签名。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _capture_input_identity(
    input_path: str,
) -> tuple[dict[str, Any], tuple[int, int, int, int, int] | None]:
    """一次性捕获输入身份和签名；哈希期间变化时不返回可复用签名。"""
    path = Path(input_path)
    identity: dict[str, Any] = {
        "name": path.name,
        "suffix": path.suffix.lower(),
        "exists": path.is_file(),
    }
    if not identity["exists"]:
        return identity, None

    before = _source_signature(path)
    try:
        if before is not None:
            identity["size_bytes"] = before[2]
        identity["sha256"] = source_sha256(str(path))
    except OSError:
        identity["readable"] = False
        return identity, None
    after = _source_signature(path)
    return identity, after if before == after else None


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
        "package_version": _package_version(),
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
        "chapters_translated": sum(item.get("status") == "done" for item in chapters),
        "segments_total": 0,
        "segments_translated": 0,
    }
    for item in chapters:
        chapter = store.load_chapter(item["index"])
        text_segments = chapter.text_segments
        summary["segments_total"] += len(text_segments)
        summary["segments_translated"] += sum(
            bool(segment.target and segment.target.strip()) for segment in text_segments
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
    invocation: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(
        default_factory=lambda: (
            f"run-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f%z')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
    )
    started_at: str = field(default_factory=_now_iso)
    _started: float = field(default_factory=time.perf_counter)
    _store: RunStore | None = None
    _stage_seconds: dict[str, float] = field(default_factory=dict)
    _stage_started: dict[str, float] = field(default_factory=dict)
    _stage_depth: dict[str, int] = field(default_factory=dict)
    _finished: bool = False
    _input_signature: tuple[int, int, int, int, int] | None = field(
        default=None,
        repr=False,
    )
    _state_snapshot: dict[str, Any] | None = field(default=None, repr=False)

    @classmethod
    def start(
        cls,
        *,
        operation: str,
        requested_steps: list[str],
        input_path: str,
        config: Config,
        client: LLMClient,
        invocation: dict[str, Any] | None = None,
    ) -> RunMetricsRecorder:
        """在任何模型调用之前抓取本次运行的基线和非配置参数。"""
        started_at = _now_iso()
        started = time.perf_counter()
        input_info, input_signature = _capture_input_identity(input_path)
        return cls(
            operation=operation,
            requested_steps=list(requested_steps),
            input=input_info,
            config=config_identity(config),
            code=code_identity(),
            usage_before=client.usage_summary(),
            invocation=_safe_value(invocation or {}),
            started_at=started_at,
            _started=started,
            _input_signature=input_signature,
        )

    def verify_input_sha256(self, input_path: str) -> str | None:
        """廉价确认源文件未变；签名改变时重新哈希并与启动快照比较。"""
        expected = self.input.get("sha256")
        if not isinstance(expected, str):
            return None
        path = Path(input_path)
        current_signature = _source_signature(path)
        # Windows st_ctime is the creation time, so same-size rewrites can keep this signature.
        if (
            os.name != "nt"
            and self._input_signature is not None
            and current_signature == self._input_signature
        ):
            return expected

        refreshed, signature = _capture_input_identity(input_path)
        actual = refreshed.get("sha256")
        if not isinstance(actual, str) or actual != expected:
            raise ValueError("源文件在本次命令执行期间发生变化；请确认文件稳定后重试。")
        self._input_signature = signature
        self.input.update(refreshed)
        return actual

    def attach_store(self, store: RunStore) -> None:
        """绑定状态目录；只接受本次操作实际使用的第一本书。"""
        if self._store is None:
            self._store = store
            return
        if self._store.run_dir != store.run_dir:
            raise ValueError("一次运行账本不能跨越多个书籍状态目录")

    def capture_state(self, store: RunStore) -> None:
        """从调用方保证一致的实时状态或只读快照冻结本次结束状态。"""
        self.attach_store(store)
        try:
            self._state_snapshot = _state_summary(store)
        except (OSError, KeyError, TypeError, ValueError):
            self._state_snapshot = {"available": False}

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
                self._stage_seconds[name] = self._stage_seconds.get(name, 0.0) + elapsed

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
            "invocation": self.invocation,
            "status": status,
            "started_at": self.started_at,
            "stage_seconds": {
                name: round(seconds, 6) for name, seconds in sorted(self._stage_seconds.items())
            },
            "input": self.input,
            "config": self.config,
            "code": self.code,
            "usage": usage_delta(client.usage_summary(), self.usage_before),
        }
        if self._state_snapshot is not None:
            record["state"] = self._state_snapshot
        else:
            try:
                record["state"] = _state_summary(self._store)
            except (OSError, KeyError, TypeError, ValueError):
                record["state"] = {"available": False}
        if error is not None:
            record["error"] = {"type": type(error).__name__}
        record["finished_at"] = _now_iso()
        record["duration_seconds"] = round(time.perf_counter() - self._started, 6)
        return self._store.save_run_metric(record)
