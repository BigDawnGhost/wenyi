"""Export submission fails cleanly without blocking active workflows."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from wenyi_api.routers import export
from wenyi_api.schemas import ExportRequest


@pytest.mark.parametrize(
    "source_fmt,meta,expected",
    [
        ("docx", {}, "docx"),
        ("srt", {}, "srt"),
        ("pdf", {"pdf_export": "babeldoc"}, "pdf"),
        ("text", {}, "epub"),
    ],
)
def test_export_defaults_and_failure_records(monkeypatch, source_fmt, meta, expected):
    from wenyi_core.config import Config

    records, statuses = [], []
    monkeypatch.setattr(
        export, "effective_config", lambda project: Config.from_dict({"llm": {"preset": "fake"}})
    )
    monkeypatch.setattr(export.dal, "create_job", lambda *args, **kwargs: 7)
    monkeypatch.setattr(export.dal, "set_job_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        export,
        "require_project",
        lambda pid: {"id": pid, "fmt": source_fmt, "initialized": True, "status": "translating"},
    )
    monkeypatch.setattr(
        export, "storage_for", lambda pid: SimpleNamespace(load_manifest=lambda: {"meta": meta})
    )
    monkeypatch.setattr(
        export.dal, "create_export", lambda pid, fmt, opts: records.append((fmt, opts)) or 9
    )
    monkeypatch.setattr(
        export.dal,
        "set_export_status",
        lambda eid, status, **kw: statuses.append((eid, status, kw)),
    )

    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(export, "enqueue", unavailable)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(export.create_export("p", ExportRequest()))
    assert raised.value.status_code == 503
    assert records[0][0] == expected
    assert statuses[0][:2] == (9, "error")
