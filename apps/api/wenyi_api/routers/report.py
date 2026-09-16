"""Current progress/review/accounting reports for books and subtitles."""

from __future__ import annotations

from fastapi import APIRouter
from wenyi_core.assemble.report import build_report
from wenyi_core.srt.store import SrtRunStore

from ..project_service import project_write, require_project, storage_for

router = APIRouter(prefix="/projects/{pid}/report", tags=["report"])


def _report(project: dict, storage) -> dict:
    # All read operations share one brief state transaction; there are no model calls.
    with storage.state_lock():
        if project.get("fmt") == "srt":
            cues = SrtRunStore(storage.run_dir, storage=storage).load_cues()
            report = {
                "summary": {
                    "cues_total": len(cues),
                    "cues_done": sum(row.get("status") == "done" for row in cues.values()),
                    "empty_targets": sum(
                        not (row.get("target") or "").strip() for row in cues.values()
                    ),
                }
            }
        else:
            report = build_report(storage, storage)
        report["usage"] = storage.load_usage() or {}
        report["timing"] = storage.read_artifact("timing.json") or {}
        return report


@router.get("")
def get_report(pid: str) -> dict:
    return _report(require_project(pid), storage_for(pid))


@router.post("")
def regenerate_report(pid: str) -> dict:
    with project_write(pid) as (project, storage):
        report = _report(project, storage)
        storage.save_report(report)
        storage.log_event("report_saved", artifact="report.json")
    return report
