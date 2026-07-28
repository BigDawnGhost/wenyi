"""章节任务路由的契约测试（不依赖 DB / Redis）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from wenyi_api.routers import chapters


def test_translate_chapter_marks_running_and_enqueues_only_that_chapter(
    monkeypatch,
):
    statuses: list[tuple[str, object]] = []
    enqueued: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        chapters.dal,
        "get_project",
        lambda pid: {"id": pid, "status": "prepared"},
    )
    monkeypatch.setattr(
        chapters.dal,
        "chapter_summaries",
        lambda pid: [{"index": 2, "status": "pending"}],
    )
    monkeypatch.setattr(
        chapters.dal,
        "set_project_status",
        lambda pid, status: statuses.append(("project", (pid, status))),
    )
    monkeypatch.setattr(
        chapters.dal,
        "set_chapter_status",
        lambda pid, ci, status: statuses.append(
            ("chapter", (pid, ci, status))
        ),
    )

    async def fake_enqueue(name: str, **kwargs):
        enqueued.append((name, kwargs))
        return SimpleNamespace(job_id="chapter-job")

    monkeypatch.setattr(chapters, "enqueue", fake_enqueue)

    result = asyncio.run(chapters.translate_chapter("project-1", 2))

    assert statuses == [
        ("project", ("project-1", "translating")),
        ("chapter", ("project-1", 2, "translating")),
    ]
    assert enqueued == [
        (
            "run_chapter_translation",
            {"project_id": "project-1", "chapter_index": 2},
        )
    ]
    assert result["kind"] == "chapter_translation"


def test_translate_chapter_rejects_completed_chapter(monkeypatch):
    monkeypatch.setattr(
        chapters.dal,
        "get_project",
        lambda pid: {"id": pid, "status": "done"},
    )
    monkeypatch.setattr(
        chapters.dal,
        "chapter_summaries",
        lambda pid: [{"index": 0, "status": "done"}],
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(chapters.translate_chapter("project-1", 0))

    assert raised.value.status_code == 409
    assert raised.value.detail == "chapter is already translated"


def test_translate_chapter_restores_status_when_enqueue_fails(monkeypatch):
    statuses: list[tuple[str, object]] = []
    monkeypatch.setattr(
        chapters.dal,
        "get_project",
        lambda pid: {"id": pid, "status": "prepared"},
    )
    monkeypatch.setattr(
        chapters.dal,
        "chapter_summaries",
        lambda pid: [{"index": 1, "status": "pending"}],
    )
    monkeypatch.setattr(
        chapters.dal,
        "set_project_status",
        lambda pid, status: statuses.append(("project", (pid, status))),
    )
    monkeypatch.setattr(
        chapters.dal,
        "set_chapter_status",
        lambda pid, ci, status: statuses.append(
            ("chapter", (pid, ci, status))
        ),
    )

    async def failing_enqueue(name: str, **kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(chapters, "enqueue", failing_enqueue)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        asyncio.run(chapters.translate_chapter("project-1", 1))

    assert statuses[-2:] == [
        ("project", ("project-1", "prepared")),
        ("chapter", ("project-1", 1, "pending")),
    ]
