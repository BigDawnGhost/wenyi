"""Migrated export raises must carry codes without changing their text."""

import pytest
from fastapi import HTTPException
from wenyi_backend.errors import (
    EXPORT_NOT_FOUND,
    INVALID_CONFIGURATION,
    QUEUE_UNAVAILABLE,
    SOURCE_NOT_READY,
    ApiError,
)
from wenyi_backend.export_plan import resolve_export
from wenyi_backend.schemas import ExportRequest


def test_uninitialised_project_refuses_export_with_a_code():
    with pytest.raises(ApiError) as caught:
        resolve_export({"fmt": "epub", "initialized": False}, {}, ExportRequest())
    assert caught.value.code == SOURCE_NOT_READY
    assert caught.value.status_code == 409
    assert caught.value.detail == "Prepare or translate the source before exporting"


def test_subtitle_and_book_formats_cannot_be_mixed():
    with pytest.raises(ApiError) as caught:
        resolve_export({"fmt": "srt", "initialized": True}, {}, ExportRequest(format="docx"))
    assert caught.value.code == INVALID_CONFIGURATION
    assert caught.value.detail == (
        "Subtitle projects export SRT; book projects export book formats"
    )


def test_writer_rejection_keeps_the_writer_message():
    """The 422 text must stay byte-identical to what the writer raised."""
    with pytest.raises(ApiError) as caught:
        resolve_export(
            {"fmt": "pdf", "initialized": True},
            {"meta": {"mineru": True}},
            ExportRequest(format="pdf"),
        )
    assert caught.value.code == INVALID_CONFIGURATION
    assert caught.value.detail == (
        "PDF export is only available for BabelDOC projects; use EPUB, DOCX or HTML instead."
    )


def test_migrated_errors_still_satisfy_except_http_exception():
    """Existing guards catch HTTPException; the code must ride along intact."""
    try:
        resolve_export({"fmt": "epub", "initialized": False}, {}, ExportRequest())
    except HTTPException as error:
        assert isinstance(error, ApiError)
        assert error.code == SOURCE_NOT_READY
    else:
        pytest.fail("expected an export rejection")


def test_export_not_found_code_is_registered_for_its_text():
    from wenyi_backend.errors import code_for

    assert code_for(404, "Completed export not found") == EXPORT_NOT_FOUND


def test_source_not_ready_is_distinct_from_queue_unavailable():
    """A missing source is not a queue failure; clients branch on the difference."""
    with pytest.raises(ApiError) as caught:
        resolve_export({"fmt": "epub", "initialized": False}, {}, ExportRequest())
    assert caught.value.code == SOURCE_NOT_READY
    assert caught.value.code != QUEUE_UNAVAILABLE


def test_export_queue_unavailable_still_uses_the_queue_code():
    from wenyi_backend.errors import code_for

    assert code_for(503, "Export queue is unavailable; retry the operation") == QUEUE_UNAVAILABLE
