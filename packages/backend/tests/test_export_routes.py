"""Export submission fails cleanly without blocking active workflows."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from wenyi_backend.routers import export
from wenyi_backend.schemas import ExportRequest


@pytest.mark.parametrize(
    "source_fmt,meta,expected",
    [
        ("docx", {}, "docx"),
        ("srt", {}, "srt"),
        ("pdf", {"pdf_export": "babeldoc"}, "pdf"),
        ("text", {}, "epub"),
    ],
)
def test_export_defaults_and_failure_records(
    monkeypatch, backend_context, source_fmt, meta, expected
):
    from wenyi_core.config import Config

    records, statuses = [], []
    monkeypatch.setattr(
        export, "effective_config", lambda project: Config.from_dict({"llm": {"preset": "fake"}})
    )
    monkeypatch.setattr(
        export,
        "require_project",
        lambda pid: {"id": pid, "fmt": source_fmt, "initialized": True, "status": "translating"},
    )
    monkeypatch.setattr(
        export,
        "storage_for",
        lambda pid: SimpleNamespace(load_manifest=lambda: {"meta": meta}, close=lambda: None),
    )
    backend_context.exports.admit_export.side_effect = lambda pid, fmt, opts, run, config: (
        records.append((fmt, opts)) or (9, 7)
    )
    backend_context.exports.fail_export_admission.side_effect = lambda job, error: statuses.append(
        (9, "error", {"error": error})
    )

    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(export, "enqueue", unavailable)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(export.create_export("p", ExportRequest()))
    assert raised.value.status_code == 503
    assert records[0][0] == expected
    assert statuses[0][:2] == (9, "error")
