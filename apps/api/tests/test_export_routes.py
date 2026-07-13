"""导出任务路由的契约测试（不依赖 DB / Redis）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from wenyi_api.routers import export
from wenyi_api.schemas import ExportRequest


def test_create_export_inserts_pending_record_before_enqueue(monkeypatch):
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(export.dal, "get_project", lambda pid: {"id": pid})

    def fake_create_export(pid: str, fmt: str, options: dict) -> int:
        calls.append(("create", (pid, fmt, options)))
        return 42

    async def fake_enqueue(name: str, **kwargs):
        calls.append(("enqueue", (name, kwargs)))
        return SimpleNamespace(job_id="export-job")

    monkeypatch.setattr(export.dal, "create_export", fake_create_export)
    monkeypatch.setattr(export, "enqueue", fake_enqueue)

    result = asyncio.run(
        export.create_export(
            "project-1",
            ExportRequest(
                format="epub",
                bilingual=True,
                order="source_first",
                about_page=False,
            ),
        )
    )

    assert calls[0] == (
        "create",
        (
            "project-1",
            "epub",
            {
                "bilingual": True,
                "order": "source_first",
                "about_page": False,
            },
        ),
    )
    assert calls[1] == (
        "enqueue",
        (
            "run_export",
            {
                "project_id": "project-1",
                "export_id": 42,
                "fmt": "epub",
                "bilingual": True,
                "order": "source_first",
                "about_page": False,
            },
        ),
    )
    assert result["job_id"] == "export-job"


def test_create_export_marks_record_error_when_enqueue_fails(monkeypatch):
    statuses: list[tuple[int, str]] = []
    monkeypatch.setattr(export.dal, "get_project", lambda pid: {"id": pid})
    monkeypatch.setattr(export.dal, "create_export", lambda pid, fmt, options: 7)
    monkeypatch.setattr(
        export.dal,
        "set_export_status",
        lambda export_id, status: statuses.append((export_id, status)),
    )

    async def failing_enqueue(name: str, **kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(export, "enqueue", failing_enqueue)

    try:
        asyncio.run(export.create_export("project-1", ExportRequest()))
    except RuntimeError as exc:
        assert str(exc) == "redis unavailable"
    else:
        raise AssertionError("enqueue failure should propagate")

    assert statuses == [(7, "error")]
