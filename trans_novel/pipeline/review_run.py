"""正式但只读的全书 Review 运行记录。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any


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

    @property
    def unresolved_issues(self) -> list[dict[str, Any]]:
        """返回因仲裁未决而禁止自动执行的问题。"""
        return list(self.result.get("unresolved_issues") or [])


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
        """在首个模型调用前创建运行中结果并保存运行参数。"""
        self._reviewed_content_digest = reviewed_content_digest
        self.write_json("rounds/metadata.json", metadata)
        self._atomic_json(
            os.path.join(self.run_dir, "result.json"),
            {
                "review_id": self.review_id,
                "status": "running",
                "termination": "not_started",
                "reviewed_content_digest": reviewed_content_digest,
                "started_at": self.started_at,
                "summary": {"issue_count": 0, "change_count": 0},
                "issues": [],
                "unresolved_issues": [],
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
        unresolved_issues: list[dict[str, Any]] | None = None,
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
            "unresolved_issues": list(unresolved_issues or []),
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
        """保存本次 Review 的 Token 增量。"""
        self._atomic_json(os.path.join(self.run_dir, "usage.json"), usage)
        self.log_event("review_usage_recorded", **usage["totals"])
