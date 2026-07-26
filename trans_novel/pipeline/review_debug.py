"""实验性 Review 的独立调试产物。

每次全书审校创建一个带时间戳的目录。并发 worker 只写各自唯一的
agent trace；共享事件流由本类加锁串行追加，避免污染正式 events.jsonl。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from threading import Lock
from typing import Any


class DebugReviewRun:
    """管理一次 Debug-only Review 的目录、事件流和原子 JSON 写入。"""

    def __init__(self, book_run_dir: str, *, now: datetime | None = None):
        moment = (now or datetime.now().astimezone()).astimezone()
        stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
        debug_root = os.path.join(book_run_dir, "debug")
        os.makedirs(debug_root, exist_ok=True)

        candidate = os.path.join(debug_root, f"review-{stamp}")
        suffix = 1
        while True:
            try:
                os.makedirs(candidate)
                break
            except FileExistsError:
                candidate = os.path.join(debug_root, f"review-{stamp}-{suffix:02d}")
                suffix += 1

        self.run_dir = candidate
        self.run_id = os.path.basename(candidate)
        self.started_at = moment.isoformat(timespec="microseconds")
        self._event_path = os.path.join(candidate, "events.jsonl")
        self._event_lock = Lock()
        self._sequence = 0
        self._result_lock = Lock()
        self._initial_issues: list[dict[str, Any]] = []
        self._dismissed_issues: list[dict[str, Any]] = []
        self._metadata: dict[str, Any] = {}

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
        return os.path.join(self.run_dir, relative)

    def write_json(self, relative: str, data: Any) -> str:
        """原子保存一个调试 JSON 并返回绝对路径。"""
        path = self.path(relative)
        self._atomic_json(path, data)
        return path

    def log_event(self, event: str, **data: Any) -> None:
        """线程安全地追加结构化调试事件。"""
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
                    "candidate_id": f"ch{chapter}-base{chunk_base}-candidate{ordinal}",
                    "chapter": chapter,
                    "index": chunk_base + index,
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
            }
            for issue in issues
            if isinstance(issue.get("index"), int) and not isinstance(issue.get("index"), bool)
        ]
        with self._result_lock:
            self._dismissed_issues.extend(rows)

    def result_snapshots(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回按书序排列的初审和驳回问题副本。"""
        with self._result_lock:
            initial = [dict(issue) for issue in self._initial_issues]
            dismissed = [dict(issue) for issue in self._dismissed_issues]

        def position(item: dict[str, Any]) -> tuple[Any, Any]:
            return item.get("chapter", -1), item.get("index", -1)

        return sorted(initial, key=position), sorted(dismissed, key=position)

    def start(self, **metadata: Any) -> None:
        """在首个模型调用前写入本次运行的初始说明。"""
        self._metadata = dict(metadata)
        self.write_json(
            "run.json",
            {
                "run_id": self.run_id,
                "status": "running",
                "started_at": self.started_at,
                **metadata,
            },
        )
        self.log_event("review_debug_started", run_id=self.run_id)

    def finish(self, *, status: str, **summary: Any) -> None:
        """更新 run.json，并保留成功或失败时的最终摘要。"""
        finished_at = datetime.now().astimezone().isoformat(timespec="microseconds")
        self.write_json(
            "run.json",
            {
                "run_id": self.run_id,
                "status": status,
                "started_at": self.started_at,
                "finished_at": finished_at,
                **self._metadata,
                **summary,
            },
        )
        self.log_event(
            "review_debug_finished",
            run_id=self.run_id,
            status=status,
            **{
                key: value
                for key, value in summary.items()
                if key
                in {
                    "issue_count",
                    "conflict_count",
                    "fallback_agent_count",
                    "error_type",
                }
            },
        )
