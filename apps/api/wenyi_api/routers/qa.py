"""跨章一致性 QA：触发检查、查询结果与重生成确定性报告。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from wenyi_core.assemble.report import build_report

from .. import dal
from ..db import get_pool
from ..qa_state import COMPLETION_STATUSES, read_qa_state, write_qa_state
from ..schemas import JobEnqueued, QAResult
from ..storage_pg import PostgresStorage
from ..workers import enqueue

router = APIRouter(prefix="/projects/{pid}", tags=["qa"])


def _project_or_404(pid: str) -> dict:
    project = dal.get_project(pid)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def _storage(pid: str) -> PostgresStorage:
    return PostgresStorage(pid, get_pool())


@router.post("/qa", response_model=JobEnqueued)
async def run_qa(pid: str) -> dict:
    """在翻译完成后投递跨章一致性检查任务。"""
    project = _project_or_404(pid)
    project_status = str(project.get("status") or "")
    if project_status not in {"done", "reviewed", "error"}:
        raise HTTPException(409, "project must be idle after translation")

    chapters = dal.chapter_summaries(pid)
    if not chapters or any(chapter["status"] != "done" for chapter in chapters):
        raise HTTPException(409, "translation must be completed before consistency QA")

    storage = _storage(pid)
    previous_report = storage.load_report() or {}
    previous_qa = read_qa_state(previous_report)
    previous_completion_status = (
        previous_qa["completion_status"]
        if previous_qa is not None
        else None
    )
    completion_status = (
        project_status
        if project_status in {"done", "reviewed"}
        else previous_completion_status
        if previous_completion_status in COMPLETION_STATUSES
        else "done"
    )
    write_qa_state(
        storage,
        status="running",
        completion_status=completion_status,
    )
    dal.set_project_status(pid, "qa")
    try:
        job = await enqueue(
            "run_qa",
            project_id=pid,
            completion_status=completion_status,
        )
    except Exception:
        storage.save_report(previous_report)
        dal.set_project_status(pid, project_status)
        raise
    return {
        "job_id": job.job_id if job else "sync",
        "project_id": pid,
        "kind": "qa",
    }


@router.get("/qa", response_model=QAResult)
def get_qa(pid: str) -> dict:
    """返回最近一次已持久化的一致性检查结果及当前运行状态。"""
    _project_or_404(pid)
    report = _storage(pid).load_report() or {}
    issues = report.get("consistency_issues")
    if not isinstance(issues, list):
        issues = []

    qa_state = read_qa_state(report)
    if qa_state is not None:
        status = qa_state["status"]
        error = qa_state.get("error")
    else:
        status = "completed" if "consistency_issues" in report else "idle"
        error = None
    return {"status": status, "issues": issues, "error": error}


@router.post("/report", response_model=dict[str, Any])
def regenerate_report(pid: str) -> dict[str, Any]:
    """根据当前数据库状态重生成报告；不会调用 LLM。"""
    _project_or_404(pid)
    storage = _storage(pid)
    if not storage.exists():
        raise HTTPException(409, "project has no translation state")

    previous = storage.load_report() or {}
    report = build_report(storage)
    for key in ("consistency_issues", "consistency_qa"):
        if key in previous:
            report[key] = previous[key]
    storage.save_report(report)
    storage.log_event("report_saved", source="web")
    return report
