"""Platform-neutral export format and optional-engine policy."""

import pytest
from fastapi import HTTPException
from wenyi_backend import export_plan
from wenyi_backend.schemas import ExportRequest


@pytest.mark.parametrize(
    "source_format,meta,expected",
    [("srt", {}, "srt"), ("docx", {}, "docx"), ("pdf", {"pdf_export": "babeldoc"}, "pdf")],
)
def test_default_plan_policy_matches_source(source_format, meta, expected):
    fmt, options = export_plan.resolve_export(
        {"initialized": True, "fmt": source_format}, {"meta": meta}, ExportRequest(bilingual=True)
    )
    assert fmt == expected
    assert options["bilingual"] is True


def test_plan_validates_subtitles_initialization_and_installed_pdf_engines(monkeypatch):
    project = {"initialized": True, "fmt": "txt"}
    for changes, request, status in [
        ({"initialized": False}, {}, 409),
        ({}, {"format": "srt"}, 422),
        ({"fmt": "srt"}, {"format": "epub"}, 422),
    ]:
        with pytest.raises(HTTPException) as error:
            export_plan.resolve_export({**project, **changes}, {}, ExportRequest(**request))
        assert error.value.status_code == status
    monkeypatch.setattr(export_plan.importlib.util, "find_spec", lambda name: name == "fpdf")
    _, options = export_plan.resolve_export(project, {}, ExportRequest(format="pdf"))
    assert options["pdf_engine"] == "fpdf2"
    with pytest.raises(HTTPException, match="not installed"):
        export_plan.resolve_export(
            project, {}, ExportRequest(format="pdf", pdf_engine="weasyprint")
        )
    monkeypatch.setattr(export_plan.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(HTTPException, match="optional pdf-export"):
        export_plan.resolve_export(project, {}, ExportRequest(format="pdf"))
