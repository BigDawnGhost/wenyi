"""正式但只读的全书 Review 运行记录，支持断点续跑。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

from ..llm.usage import merge_usage_summaries


def review_candidate_id(
    chapter: int,
    chunk_base: int,
    ordinal: int,
    review_round: int | None = None,
) -> str:
    """生成初审快照与 Agent 协议共用的确定性候选 ID。"""
    prefix = f"r{review_round}-" if review_round is not None else ""
    return f"{prefix}ch{chapter}-base{chunk_base}-candidate{ordinal}"


@dataclass(frozen=True)
class ReviewOutcome:
    """一次已完成 Review 的正式结果及目录。"""

    run_dir: str
    result: dict[str, Any]
    usage: dict[str, Any]

    @property
    def issues(self) -> list[dict[str, Any]]:
        """返回盲复审结束后仍存在的问题。"""
        return list(self.result.get("issues") or [])

    @property
    def changes(self) -> list[dict[str, Any]]:
        """返回折叠后的最终影子修改建议。"""
        return list(self.result.get("changes") or [])


class ReviewRunStore:
    """管理一次只读 Review 的结果、事件与逐轮记录。"""

    def __init__(self, book_run_dir: str, *, now: datetime | None = None):
        moment = (now or datetime.now().astimezone()).astimezone()
        stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
        review_root = os.path.join(book_run_dir, "reviews")
        os.makedirs(review_root, exist_ok=True)

        candidate = os.path.join(review_root, f"review-{stamp}")
        suffix = 1
        while True:
            try:
                os.makedirs(candidate)
                break
            except FileExistsError:
                candidate = os.path.join(review_root, f"review-{stamp}-{suffix:02d}")
                suffix += 1

        self.run_dir = candidate
        self.review_id = os.path.basename(candidate)
        self.started_at = moment.isoformat(timespec="microseconds")
        self._event_path = os.path.join(candidate, "events.jsonl")
        self._event_lock = Lock()
        self._sequence = 0
        self._result_lock = Lock()
        self._initial_issues: list[dict[str, Any]] = []
        self._dismissed_issues: list[dict[str, Any]] = []
        self._active_round: int | None = None
        self._reviewed_content_digest = ""

    @staticmethod
    def _atomic_json(path: str, data: Any) -> None:
        """把 JSON 原子写入目标路径，避免中断留下半个文件。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def path(self, relative: str) -> str:
        """返回本次 Review 目录内的绝对路径。"""
        if self._active_round is not None:
            relative = f"rounds/{self._active_round:03d}/{relative}"
        return os.path.join(self.run_dir, relative)

    @contextmanager
    def round_scope(self, round_number: int) -> Iterator[None]:
        """把并发 trace 与阶段产物隔离到指定 Review 轮次。"""
        if self._active_round is not None:
            raise RuntimeError("Review round scopes cannot be nested")
        self._active_round = round_number
        try:
            yield
        finally:
            self._active_round = None

    def write_json(self, relative: str, data: Any) -> str:
        """原子保存一个逐轮 JSON 并返回绝对路径。"""
        path = self.path(relative)
        self._atomic_json(path, data)
        return path

    def load_json(self, relative: str) -> dict[str, Any] | None:
        """按 round 作用域读取一个逐轮 JSON；缺失或损坏时返回 None。"""
        path = self.path(relative)
        try:
            with open(path, encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            return None

    def log_event(self, event: str, **data: Any) -> None:
        """线程安全地追加本次 Review 的结构化事件。"""
        if self._active_round is not None:
            data.setdefault("review_round", self._active_round)
        with self._event_lock:
            self._sequence += 1
            row = {
                "seq": self._sequence,
                "ts": datetime.now().astimezone().isoformat(timespec="microseconds"),
                "event": event,
                **data,
            }
            with open(self._event_path, "a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def record_initial_issues(
        self,
        *,
        chapter: int,
        chunk_base: int,
        issues: list[dict[str, Any]],
    ) -> None:
        """线程安全地汇总成功叶块的初审候选。"""
        rows = []
        for ordinal, issue in enumerate(issues):
            index = issue.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            rows.append(
                {
                    **dict(issue),
                    "candidate_id": review_candidate_id(
                        chapter,
                        chunk_base,
                        ordinal,
                        self._active_round,
                    ),
                    "chapter": chapter,
                    "index": chunk_base + index,
                    **(
                        {"review_round": self._active_round}
                        if self._active_round is not None
                        else {}
                    ),
                }
            )
        with self._result_lock:
            self._initial_issues.extend(rows)

    def record_dismissed(
        self,
        *,
        chapter: int,
        chunk_base: int,
        issues: list[dict[str, Any]],
    ) -> None:
        """线程安全地汇总被块级 Agent 驳回的候选。"""
        rows = [
            {
                **dict(issue),
                "chapter": chapter,
                "index": chunk_base + int(issue["index"]),
                **({"review_round": self._active_round} if self._active_round is not None else {}),
            }
            for issue in issues
            if isinstance(issue.get("index"), int) and not isinstance(issue.get("index"), bool)
        ]
        with self._result_lock:
            self._dismissed_issues.extend(rows)

    def result_snapshots(
        self,
        round_number: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回按轮次和书序排列的初审、驳回问题副本。"""
        with self._result_lock:
            initial = [
                dict(issue)
                for issue in self._initial_issues
                if round_number is None or issue.get("review_round") == round_number
            ]
            dismissed = [
                dict(issue)
                for issue in self._dismissed_issues
                if round_number is None or issue.get("review_round") == round_number
            ]

        def position(item: dict[str, Any]) -> tuple[Any, Any, Any]:
            return (
                item.get("review_round", -1),
                item.get("chapter", -1),
                item.get("index", -1),
            )

        return sorted(initial, key=position), sorted(dismissed, key=position)

    def start(self, *, reviewed_content_digest: str, metadata: dict[str, Any]) -> None:
        """在首个模型调用前创建运行中结果并保存运行参数。

        如果是续跑（status=running），保留已有结果和 metadata 不覆盖。
        """
        self._reviewed_content_digest = reviewed_content_digest
        result_path = os.path.join(self.run_dir, "result.json")
        if os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if existing.get("status") == "running":
                    # 续跑：保留已有结果和 metadata，只更新时间戳
                    existing["resumed_at"] = (
                        datetime.now().astimezone().isoformat(timespec="microseconds")
                    )
                    self._atomic_json(result_path, existing)
                    self.log_event("review_resumed", review_id=self.review_id)
                    return
            except (json.JSONDecodeError, OSError):
                pass

        # 首次运行：保存 metadata 并创建新结果
        metadata["reviewed_content_digest"] = reviewed_content_digest
        self.write_json("rounds/metadata.json", metadata)
        self._atomic_json(
            result_path,
            {
                "review_id": self.review_id,
                "status": "running",
                "termination": "not_started",
                "reviewed_content_digest": reviewed_content_digest,
                "started_at": self.started_at,
                "summary": {"issue_count": 0, "change_count": 0},
                "issues": [],
                "changes": [],
            },
        )
        self.log_event("review_started", review_id=self.review_id)

    def finish(
        self,
        *,
        status: str,
        termination: str,
        summary: dict[str, Any],
        issues: list[dict[str, Any]],
        changes: list[dict[str, Any]],
        error: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """写入最终统一结果并返回其内存副本。"""
        result: dict[str, Any] = {
            "review_id": self.review_id,
            "status": status,
            "termination": termination,
            "reviewed_content_digest": self._reviewed_content_digest,
            "started_at": self.started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="microseconds"),
            "summary": dict(summary),
            "issues": list(issues),
            "changes": list(changes),
        }
        if error is not None:
            result["error"] = dict(error)
        self._atomic_json(os.path.join(self.run_dir, "result.json"), result)
        self.log_event(
            "review_finished",
            review_id=self.review_id,
            status=status,
            termination=termination,
            issue_count=len(issues),
            change_count=len(changes),
        )
        return result

    def save_usage(self, usage: dict[str, Any]) -> None:
        """合并保存本次 Review 的 Token 增量（跨进程续跑不丢失）。"""
        existing = self.load_usage()
        if existing is not None:
            usage = merge_usage_summaries(existing, usage)
        self._atomic_json(os.path.join(self.run_dir, "usage.json"), usage)
        self.log_event("review_usage_recorded", **usage["totals"])

    def load_usage(self) -> dict[str, Any] | None:
        """读取已落盘的 Review 用量；文件缺失时返回 None。"""
        path = os.path.join(self.run_dir, "usage.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    # ── 断点续跑：chunk 级别完成状态 ──────────────────────────────────────

    def mark_chunk_done(self, chunk_id: str, result: dict[str, Any]) -> None:
        """标记一个审校块为已完成，保存结果用于续跑。"""
        chunks_dir = os.path.join(self.run_dir, "chunks")
        os.makedirs(chunks_dir, exist_ok=True)
        self._atomic_json(os.path.join(chunks_dir, f"{chunk_id}.json"), result)

    def load_chunk_result(self, chunk_id: str) -> dict[str, Any] | None:
        """加载已完成的审校块结果，未完成返回 None。"""
        path = os.path.join(self.run_dir, "chunks", f"{chunk_id}.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def is_chunk_done(self, chunk_id: str) -> bool:
        """检查审校块是否已完成。"""
        return self.load_chunk_result(chunk_id) is not None

    def rebuild_snapshots_from_chunks(self, review_round: int) -> None:
        """从已落盘的 chunk 缓存重建初审/驳回快照。

        用于 scan_done 断点续跑：跳过整轮扫描后，review_once 不再
        重放 record_initial_issues/record_dismissed，这里按 chunk 文件
        恢复内存聚合状态，保证最终报告的数据完整。

        大块优先重建；若父块与拆半残留的子块同时存在（如整块重跑
        成功后旧叶子未清理），跳过完全包含在已记录块内的子块，避免重复计数。
        """
        chunks_dir = os.path.join(self.run_dir, "chunks")
        if not os.path.isdir(chunks_dir):
            return
        prefix = f"r{review_round}-ch"
        entries: list[tuple[int, int, int, dict[str, Any]]] = []
        for name in os.listdir(chunks_dir):
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            cached = self.load_chunk_result(name[:-5])
            if cached is None:
                continue
            tail = name[len(prefix) :]
            try:
                chapter_part, rest = tail.split("-base", 1)
                chapter = int(chapter_part)
                base_part, n_part = rest.split("-n", 1)
                chunk_base = int(base_part)
                size = int(n_part[:-5])
            except ValueError:
                continue
            entries.append((chapter, chunk_base, size, cached))
        # 按 (chapter, -size, base) 排序：大块在前，小块的包含关系可判
        entries.sort(key=lambda e: (e[0], -e[2], e[1]))
        covered: dict[int, list[tuple[int, int]]] = {}
        for chapter, chunk_base, size, cached in entries:
            start, end = chunk_base, chunk_base + size
            ranges = covered.setdefault(chapter, [])
            if any(lo <= start and end <= hi for lo, hi in ranges):
                continue
            ranges.append((start, end))
            initial_issues = cached.get("initial_issues", [])
            if initial_issues:
                self.record_initial_issues(
                    chapter=chapter,
                    chunk_base=chunk_base,
                    issues=initial_issues,
                )
            dismissed = cached.get("dismissed", [])
            if dismissed:
                self.record_dismissed(
                    chapter=chapter,
                    chunk_base=chunk_base,
                    issues=dismissed,
                )

    # ── 断点续跑：轮级检查点 ────────────────────────────────────────────

    def save_checkpoint(self, state: dict[str, Any]) -> None:
        """保存轮级检查点，用于续跑恢复 round 循环状态。"""
        self._atomic_json(os.path.join(self.run_dir, "checkpoint.json"), state)

    def load_checkpoint(self) -> dict[str, Any] | None:
        """加载轮级检查点，不存在返回 None。"""
        path = os.path.join(self.run_dir, "checkpoint.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def find_resumable(
        book_run_dir: str,
        content_digest: str | None = None,
        *,
        config: dict[str, Any] | None = None,
        glossary_fingerprint: str | None = None,
    ) -> "ReviewRunStore | None":
        """找到最近一次未完成的 Review 用于续跑，没有则返回 None。

        ``content_digest`` / ``config`` / ``glossary_fingerprint`` 若提供，
        必须与该次 Review 的 metadata 一致，避免改配置或术语后续跑复用陈旧缓存。
        """
        review_root = os.path.join(book_run_dir, "reviews")
        if not os.path.isdir(review_root):
            return None
        candidates = sorted(
            (d for d in os.listdir(review_root) if d.startswith("review-")),
            reverse=True,
        )
        for name in candidates:
            run_dir = os.path.join(review_root, name)
            result_path = os.path.join(run_dir, "result.json")
            if not os.path.isfile(result_path):
                continue
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    result = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if result.get("status") != "running":
                continue
            need_meta = (
                content_digest is not None or config is not None or glossary_fingerprint is not None
            )
            if need_meta:
                meta_path = os.path.join(run_dir, "rounds", "metadata.json")
                if not os.path.isfile(meta_path):
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue
                if (
                    content_digest is not None
                    and meta.get("reviewed_content_digest") != content_digest
                ):
                    continue
                if config is not None and meta.get("config") != config:
                    continue
                if (
                    glossary_fingerprint is not None
                    and meta.get("glossary_fingerprint") != glossary_fingerprint
                ):
                    continue
            return ReviewRunStore._from_existing(run_dir, name)
        return None

    @classmethod
    def _from_existing(cls, run_dir: str, review_id: str) -> "ReviewRunStore":
        """从已有目录恢复一个 ReviewRunStore 实例。"""
        inst = cls.__new__(cls)
        inst.run_dir = run_dir
        inst.review_id = review_id
        inst._event_path = os.path.join(run_dir, "events.jsonl")
        inst._event_lock = Lock()
        inst._result_lock = Lock()
        inst._initial_issues = []
        inst._dismissed_issues = []
        inst._active_round = None
        inst._reviewed_content_digest = ""
        # 恢复事件序号（避免与已有事件重叠）
        inst._sequence = cls._read_max_seq(inst._event_path)
        # 从 result.json 恢复 started_at；缺失则留空。
        inst.started_at = ""
        result_path = os.path.join(run_dir, "result.json")
        if os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                inst.started_at = existing.get("started_at", "")
            except (json.JSONDecodeError, OSError):
                pass
        return inst

    @staticmethod
    def _read_max_seq(event_path: str) -> int:
        """从 events.jsonl 读取最大 seq 值。"""
        max_seq = 0
        if not os.path.isfile(event_path):
            return max_seq
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        seq = row.get("seq", 0)
                        if isinstance(seq, int) and seq > max_seq:
                            max_seq = seq
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            pass
        return max_seq
