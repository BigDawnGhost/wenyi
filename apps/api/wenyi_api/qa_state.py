"""持久化一致性 QA 状态的共享类型与转换函数。"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict, cast

QAStatus = Literal["running", "completed", "error"]
CompletionStatus = Literal["done", "reviewed"]

QA_STATUSES = frozenset({"running", "completed", "error"})
COMPLETION_STATUSES = frozenset({"done", "reviewed"})


class ConsistencyQAState(TypedDict, total=False):
    status: QAStatus
    completion_status: CompletionStatus
    error: str


class ReportStorage(Protocol):
    def load_report(self) -> dict | None: ...
    def save_report(self, data: dict) -> None: ...


def read_qa_state(report: dict) -> ConsistencyQAState | None:
    raw = report.get("consistency_qa")
    if not isinstance(raw, dict) or raw.get("status") not in QA_STATUSES:
        return None

    state: ConsistencyQAState = {
        "status": cast(QAStatus, raw["status"]),
        "completion_status": cast(
            CompletionStatus,
            raw.get("completion_status")
            if raw.get("completion_status") in COMPLETION_STATUSES
            else "done",
        ),
    }
    if isinstance(raw.get("error"), str):
        state["error"] = raw["error"]
    return state


def write_qa_state(
    storage: ReportStorage,
    *,
    status: QAStatus,
    completion_status: CompletionStatus,
    error: str | None = None,
) -> dict:
    report = storage.load_report() or {}
    state: ConsistencyQAState = {
        "status": status,
        "completion_status": completion_status,
    }
    if error:
        state["error"] = error
    report["consistency_qa"] = state
    storage.save_report(report)
    return report
