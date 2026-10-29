"""Project format choices and request validation use the same saved-state policy."""

from types import SimpleNamespace

import pytest
from wenyi_backend.routers import configuration, projects
from wenyi_core.assemble.writer_common import output_formats


@pytest.mark.parametrize(
    "source_fmt,meta",
    [
        ("epub", {}),
        ("pdf", {"mineru": True}),
        ("pdf", {"pdf_export": "babeldoc"}),
        ("pdf", {"babeldoc": True}),
        ("srt", {}),
    ],
)
def test_project_formats_follow_manifest_not_current_parser(monkeypatch, source_fmt, meta):
    project = {"id": "p", "fmt": source_fmt, "initialized": True}
    monkeypatch.setattr(projects, "require_project", lambda pid: project.copy())
    monkeypatch.setattr(projects, "chapter_summaries", lambda project: [])
    closed = []
    monkeypatch.setattr(
        projects,
        "read_storage_for",
        lambda pid: SimpleNamespace(
            load_manifest=lambda: {"meta": meta}, close=lambda: closed.append(pid)
        ),
    )

    result = projects.get_project("p")

    assert result["output_formats"] == (
        ["srt"] if source_fmt == "srt" else output_formats({"meta": meta})
    )
    assert ("pdf" in result["output_formats"]) == (
        meta.get("pdf_export") == "babeldoc" or bool(meta.get("babeldoc"))
    )
    assert closed == ([] if source_fmt == "srt" else ["p"])


def test_capabilities_advertise_only_babeldoc_pdf_export():
    assert configuration.capabilities()["pdf"] == {
        "backends": ["mineru", "babeldoc"],
        "export_backends": ["babeldoc"],
    }
