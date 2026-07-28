"""一致性 QA 与报告路由契约测试（不依赖 DB / Redis）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from wenyi_api.routers import qa


def test_run_qa_requires_completed_translation(monkeypatch):
    monkeypatch.setattr(qa.dal, "get_project", lambda pid: {"id": pid, "status": "done"})
    monkeypatch.setattr(
        qa.dal,
        "chapter_summaries",
        lambda pid: [{"index": 0, "status": "done"}, {"index": 1, "status": "pending"}],
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(qa.run_qa("project-1"))

    assert raised.value.status_code == 409


def test_run_qa_rejects_overlapping_pipeline(monkeypatch):
    monkeypatch.setattr(
        qa.dal,
        "get_project",
        lambda pid: {"id": pid, "status": "reviewing"},
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(qa.run_qa("project-1"))

    assert raised.value.status_code == 409
    assert raised.value.detail == "project must be idle after translation"


def test_run_qa_sets_status_and_enqueues_worker(monkeypatch):
    statuses: list[tuple[str, str]] = []
    enqueued: list[tuple[str, dict]] = []
    saved_reports: list[dict] = []
    monkeypatch.setattr(
        qa.dal,
        "get_project",
        lambda pid: {"id": pid, "status": "reviewed"},
    )
    monkeypatch.setattr(
        qa.dal,
        "chapter_summaries",
        lambda pid: [{"index": 0, "status": "done"}],
    )
    monkeypatch.setattr(
        qa.dal,
        "set_project_status",
        lambda pid, status: statuses.append((pid, status)),
    )
    monkeypatch.setattr(
        qa,
        "_storage",
        lambda pid: SimpleNamespace(
            load_report=lambda: {"summary": {"chapters_done": 1}},
            save_report=lambda report: saved_reports.append(report),
        ),
    )

    async def fake_enqueue(name: str, **kwargs):
        enqueued.append((name, kwargs))
        return SimpleNamespace(job_id="qa-job")

    monkeypatch.setattr(qa, "enqueue", fake_enqueue)

    result = asyncio.run(qa.run_qa("project-1"))

    assert statuses == [("project-1", "qa")]
    assert enqueued == [
        (
            "run_qa",
            {
                "project_id": "project-1",
                "completion_status": "reviewed",
            },
        )
    ]
    assert saved_reports == [
        {
            "summary": {"chapters_done": 1},
            "consistency_qa": {
                "status": "running",
                "completion_status": "reviewed",
            },
        }
    ]
    assert result == {"job_id": "qa-job", "project_id": "project-1", "kind": "qa"}


def test_get_qa_returns_persisted_results(monkeypatch):
    issues = [
        {"type": "terminology", "detail": "译名不一致", "where": "第一、三章"},
    ]
    monkeypatch.setattr(qa.dal, "get_project", lambda pid: {"id": pid, "status": "done"})
    monkeypatch.setattr(
        qa,
        "_storage",
        lambda pid: SimpleNamespace(
            load_report=lambda: {"consistency_issues": issues},
        ),
    )

    assert qa.get_qa("project-1") == {
        "status": "completed",
        "issues": issues,
        "error": None,
    }


def test_get_qa_reports_failed_rerun_without_hiding_previous_results(monkeypatch):
    issues = [{"type": "punctuation", "detail": "标点不一致", "where": "第一章"}]
    monkeypatch.setattr(qa.dal, "get_project", lambda pid: {"id": pid, "status": "done"})
    monkeypatch.setattr(
        qa,
        "_storage",
        lambda pid: SimpleNamespace(
            load_report=lambda: {
                "consistency_issues": issues,
                "consistency_qa": {
                    "status": "error",
                    "error": "LLM unavailable",
                },
            },
        ),
    )

    assert qa.get_qa("project-1") == {
        "status": "error",
        "issues": issues,
        "error": "LLM unavailable",
    }


def test_get_qa_does_not_treat_unrelated_project_error_as_qa_failure(monkeypatch):
    monkeypatch.setattr(
        qa.dal,
        "get_project",
        lambda pid: {"id": pid, "status": "error"},
    )
    monkeypatch.setattr(
        qa,
        "_storage",
        lambda pid: SimpleNamespace(load_report=lambda: {}),
    )

    assert qa.get_qa("project-1") == {
        "status": "idle",
        "issues": [],
        "error": None,
    }


def test_regenerate_report_preserves_consistency_issues(monkeypatch):
    saved: list[dict] = []
    events: list[tuple[str, dict]] = []
    issues = [{"type": "tone", "detail": "语气漂移", "where": "第二章"}]
    qa_state = {"status": "completed", "completion_status": "done"}
    storage = SimpleNamespace(
        exists=lambda: True,
        load_report=lambda: {
            "consistency_issues": issues,
            "consistency_qa": qa_state,
            "stale": True,
        },
        save_report=lambda report: saved.append(report),
        log_event=lambda event, **data: events.append((event, data)),
    )
    monkeypatch.setattr(qa.dal, "get_project", lambda pid: {"id": pid, "status": "done"})
    monkeypatch.setattr(qa, "_storage", lambda pid: storage)
    monkeypatch.setattr(
        qa,
        "build_report",
        lambda current_storage: {"summary": {"chapters_done": 2}},
    )

    report = qa.regenerate_report("project-1")

    assert report == {
        "summary": {"chapters_done": 2},
        "consistency_issues": issues,
        "consistency_qa": qa_state,
    }
    assert saved == [report]
    assert events == [("report_saved", {"source": "web"})]
