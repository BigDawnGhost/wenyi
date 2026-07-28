"""项目启动路由的契约测试（不依赖 DB / Redis）。"""

from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from wenyi_api.routers import projects
from wenyi_api.schemas import StartTranslation
from wenyi_core.ingest.models import Chapter, Document, Segment


def test_start_translation_persists_selected_strategy(monkeypatch):
    saved: list[tuple[str, dict]] = []
    enqueued: list[tuple[str, dict]] = []

    monkeypatch.setattr(projects.dal, "get_project", lambda pid: {"id": pid})
    monkeypatch.setattr(
        projects.dal,
        "set_project_strategy",
        lambda pid, strategy: saved.append((pid, strategy)),
    )
    monkeypatch.setattr(projects, "set_project_status", lambda pid, status: None)

    async def fake_enqueue(name: str, **kwargs):
        enqueued.append((name, kwargs))
        return SimpleNamespace(job_id="job-1")

    monkeypatch.setattr(projects, "enqueue", fake_enqueue)

    result = asyncio.run(
        projects.start_translation(
            "project-1",
            StartTranslation(
                strategy={"template": "精翻"},
                do_qa=True,
            ),
        )
    )

    assert saved == [("project-1", {"template": "精翻"})]
    assert enqueued == [
        ("run_translation", {"project_id": "project-1", "do_qa": True})
    ]
    assert result["job_id"] == "job-1"


def test_start_translation_without_body_keeps_existing_strategy(monkeypatch):
    monkeypatch.setattr(projects.dal, "get_project", lambda pid: {"id": pid})
    monkeypatch.setattr(
        projects.dal,
        "set_project_strategy",
        lambda pid, strategy: (_ for _ in ()).throw(AssertionError("unexpected update")),
    )
    monkeypatch.setattr(projects, "set_project_status", lambda pid, status: None)

    async def fake_enqueue(name: str, **kwargs):
        return SimpleNamespace(job_id="job-2")

    monkeypatch.setattr(projects, "enqueue", fake_enqueue)

    result = asyncio.run(projects.start_translation("project-1"))

    assert result["job_id"] == "job-2"


def _stub_upload_dependencies(monkeypatch, tmp_path):
    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(projects.dal, "get_project", lambda pid: {"id": pid})
    monkeypatch.setattr(
        projects.dal,
        "set_project_source",
        lambda pid, path, title: saved.append((pid, path, title)),
    )
    monkeypatch.setattr(
        projects.paths,
        "source_path",
        lambda pid, fmt: str(tmp_path / f"source.{fmt}"),
    )
    monkeypatch.setattr(
        projects.paths,
        "source_cache_dir",
        lambda pid: str(tmp_path / "source"),
    )
    monkeypatch.setattr(
        projects,
        "settings",
        SimpleNamespace(data_dir=str(tmp_path)),
    )
    return saved


def test_upload_html_returns_chapter_preview(monkeypatch, tmp_path):
    saved = _stub_upload_dependencies(monkeypatch, tmp_path)
    source = b"""
        <html><body>
        <h1>Chapter One</h1><p>First paragraph.</p>
        <h2>Chapter Two</h2><p>Second paragraph.</p>
        </body></html>
    """

    result = projects.upload_source(
        "project-1",
        UploadFile(filename="book.html", file=BytesIO(source)),
        fmt=None,
    )

    assert result["fmt"] == "html"
    assert result["chapter_count"] == 2
    assert [chapter["title"] for chapter in result["chapters"]] == [
        "Chapter One",
        "Chapter Two",
    ]
    assert saved == [("project-1", "source.html", "source")]


def test_upload_pdf_invalidates_stale_conversion_cache(monkeypatch, tmp_path):
    from wenyi_core.ingest import segmenter

    saved = _stub_upload_dependencies(monkeypatch, tmp_path)
    cache_dir = tmp_path / "source"
    cache_dir.mkdir()
    (cache_dir / "converted.html").write_text(
        "<html><body><h1>PDF Chapter</h1><p>Body.</p></body></html>",
        encoding="utf-8",
    )

    def load_pdf(path, source_lang, target_lang, *, cache_dir):
        assert not (tmp_path / "source" / "converted.html").exists()
        return Document(
            title="novel",
            source_lang=source_lang,
            target_lang=target_lang,
            fmt="pdf",
            source_path=path,
            chapters=[
                Chapter(
                    index=0,
                    title="New PDF Chapter",
                    segments=[Segment(index=0, source="New body.")],
                )
            ],
        )

    monkeypatch.setattr(segmenter, "load_document", load_pdf)

    result = projects.upload_source(
        "project-1",
        UploadFile(filename="novel.pdf", file=BytesIO(b"new PDF")),
        fmt=None,
    )

    assert result["fmt"] == "pdf"
    assert result["title"] == "novel"
    assert result["chapter_count"] == 1
    assert result["chapters"][0]["title"] == "New PDF Chapter"
    assert saved == [("project-1", "source.pdf", "novel")]


def test_upload_pdf_without_mineru_key_has_actionable_error(
    monkeypatch, tmp_path
):
    saved = _stub_upload_dependencies(monkeypatch, tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)

    with pytest.raises(HTTPException) as raised:
        projects.upload_source(
            "project-1",
            UploadFile(filename="novel.pdf", file=BytesIO(b"not parsed")),
            fmt=None,
        )

    assert raised.value.status_code == 422
    assert raised.value.detail == (
        "解析失败：PDF 解析服务尚未配置 MINERU_API_KEY，"
        "请联系管理员配置后重试"
    )
    assert saved == []
