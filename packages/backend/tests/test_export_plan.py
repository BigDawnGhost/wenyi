"""Platform-neutral export format and BabelDOC-only PDF policy."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from wenyi_backend import export_plan
from wenyi_backend.schemas import ExportRequest


@pytest.mark.parametrize(
    "source_format,meta,expected",
    [
        ("epub", {}, "epub"),
        ("docx", {}, "docx"),
        ("html", {}, "html"),
        ("pdf", {"pdf_export": "babeldoc"}, "pdf"),
    ],
)
def test_default_plan_policy_matches_source(source_format, meta, expected):
    fmt, options = export_plan.resolve_export(
        {"initialized": True, "fmt": source_format},
        {"fmt": source_format, "meta": meta},
        ExportRequest(bilingual=True),
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


def test_plan_requires_initialization():
    with pytest.raises(HTTPException) as error:
        export_plan.resolve_export({"initialized": False, "fmt": "txt"}, {}, ExportRequest())
    assert error.value.status_code == 409


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


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("book.tar.gz", "book"),
        ("book.txt", "book"),
        # A trailing dot is discarded by the filesystem, so "book." and "book"
        # name one file and the plan has to say which of them it means.
        ("book.", "book"),
        ("book .txt", "book"),
        # A reserved device name is refused by Windows no matter what follows it.
        ("CON", "_CON"),
        ("CON.txt", "_CON"),
        ("nul", "_nul"),
        # Only an exact device name is reserved; these are ordinary files.
        ("CONSOLE.txt", "CONSOLE"),
        ("COM10.txt", "COM10"),
        ("", "translation"),
        (".", "translation"),
    ],
)
def test_export_filename_reduces_the_stem_to_a_windows_safe_name(filename, expected):
    """The target name is the last place a bad source name can be repaired.

    The check is a pure function on purpose: this drive is ReFS, which accepts
    names NTFS refuses, so relying on the write failing would be green here and
    red on a user's NTFS machine.
    """
    project = {"source_meta": {"original_filename": filename}}
    assert export_plan.export_stem(filename) == expected
    assert export_plan.export_filename(project, "zh", "docx", False) == f"{expected}.zh.docx"


def test_export_filename_without_source_metadata_stays_usable():
    """A project that never recorded a source name still gets a nameable export."""
    assert export_plan.export_filename({}, "zh", "docx", True) == "translation.zh-bi.docx"
