"""项目：创建 / 列表 / 详情 / 上传预览 / 启动翻译 / 暂停 / 恢复。"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import dal, paths
from ..config import settings
from ..dal import set_project_status
from ..schemas import (
    JobEnqueued,
    Message,
    Project,
    ProjectCreate,
    ProjectDetail,
    StartTranslation,
    UploadPreview,
)
from ..workers import enqueue

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[Project])
def list_projects() -> list[dict]:
    return dal.list_projects()


@router.post("", response_model=Project, status_code=201)
def create_project(body: ProjectCreate) -> dict:
    pid = dal.create_project(body.name, body.source_lang, body.target_lang,
                             body.strategy)
    p = dal.get_project(pid)
    assert p is not None
    return p


@router.get("/{pid}", response_model=ProjectDetail)
def get_project(pid: str) -> dict:
    p = dal.get_project(pid)
    if p is None:
        raise HTTPException(404, "project not found")
    summaries = dal.chapter_summaries(pid)
    done = sum(1 for c in summaries if c["status"] == "done")
    p.update({
        "chapter_count": len(summaries),
        "total_word_count": dal.total_word_count(pid),
        "done_chapters": done,
    })
    return p


@router.post("/{pid}/upload", response_model=UploadPreview)
def upload_source(pid: str, file: UploadFile = File(...),
                  fmt: Optional[str] = Form(None)) -> dict:
    """上传原文并解析预览（不落库章节；首次翻译时内核负责初始化）。"""
    if dal.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    filename = file.filename or "source"
    ext = fmt or os.path.splitext(filename)[1].lstrip(".").lower()
    fmt_code = "epub" if ext == "epub" else ("fb2" if ext == "fb2" else "text")
    dest = paths.source_path(pid, fmt_code)
    with open(dest, "wb") as f:
        f.write(file.file.read())

    from wenyi_core.ingest.segmenter import load_document

    try:
        doc = load_document(dest, "auto", "zh")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"解析失败：{e}") from e

    chapters = [
        {"index": c.index, "title": c.title,
         "word_count": sum(1 for s in c.text_segments if s.source.strip())}
        for c in doc.chapters
    ]
    total = sum(c["word_count"] for c in chapters)
    dal.set_project_source(pid, os.path.relpath(dest, settings.data_dir), doc.title)
    return {
        "title": doc.title, "fmt": doc.fmt, "chapter_count": len(doc.chapters),
        "total_word_count": total, "source_lang": doc.source_lang or None,
        "chapters": chapters,
    }


@router.post("/{pid}/translate", response_model=JobEnqueued)
async def start_translation(pid: str, body: StartTranslation | None = None) -> dict:
    if dal.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    if body is not None and body.strategy is not None:
        dal.set_project_strategy(pid, body.strategy)
    set_project_status(pid, "translating")
    job = await enqueue(
        "run_translation",
        project_id=pid,
        do_qa=body.do_qa if body is not None else None,
    )
    return {"job_id": job.job_id if job else "sync", "project_id": pid, "kind": "translation"}


@router.post("/{pid}/pause", response_model=Message)
def pause(pid: str) -> dict:
    set_project_status(pid, "paused")
    return {"message": "paused"}


@router.post("/{pid}/resume", response_model=JobEnqueued)
async def resume(pid: str) -> dict:
    if dal.get_project(pid) is None:
        raise HTTPException(404, "project not found")
    set_project_status(pid, "translating")
    job = await enqueue("run_translation", project_id=pid)
    return {"job_id": job.job_id if job else "sync", "project_id": pid, "kind": "translation"}


@router.delete("/{pid}", response_model=Message)
def delete_project(pid: str) -> dict:
    from ..db import get_pool
    with get_pool().connection() as c:
        c.execute("DELETE FROM projects WHERE id=%s", (pid,))
    return {"message": "deleted"}
