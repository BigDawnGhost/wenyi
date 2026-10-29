"""Platform-neutral export format and BabelDOC-only PDF policy."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
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


@pytest.mark.parametrize("source_format", ["pdf", "epub", "txt", "docx", "html"])
@pytest.mark.parametrize("meta", [None, {}, {"mineru": True}, {"babeldoc": False}])
def test_pdf_export_requires_saved_babeldoc_metadata(source_format, meta):
    with pytest.raises(HTTPException, match="PDF export is only available for BabelDOC") as error:
        export_plan.resolve_export(
            {"initialized": True, "fmt": source_format},
            {"meta": meta},
            ExportRequest(format="pdf"),
        )
    assert error.value.status_code == 422


def test_plan_validates_subtitles_and_initialization():
    project = {"initialized": True, "fmt": "txt"}
    for changes, request, status in [
        ({"initialized": False}, {}, 409),
        ({}, {"format": "srt"}, 422),
        ({"fmt": "srt"}, {"format": "epub"}, 422),
    ]:
        with pytest.raises(HTTPException) as error:
            export_plan.resolve_export({**project, **changes}, {}, ExportRequest(**request))
        assert error.value.status_code == status


@pytest.mark.parametrize("meta", [{"pdf_export": "babeldoc"}, {"babeldoc": True}])
def test_babeldoc_pdf_export_needs_no_local_rendering_engine(meta):
    fmt, options = export_plan.resolve_export(
        {"initialized": True, "fmt": "pdf"}, {"meta": meta}, ExportRequest(format="pdf")
    )
    assert fmt == "pdf"
    assert "pdf_engine" not in options


def test_removed_pdf_engine_request_field_is_rejected():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExportRequest.model_validate({"format": "pdf", "pdf_engine": "removed-engine"})
