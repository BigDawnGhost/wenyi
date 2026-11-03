"""Upload guard tests (audit C-2 / worklist W-1)."""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import HTTPException
from wenyi_backend.errors import SOURCE_TOO_LARGE, ApiError
from wenyi_backend.source_upload import MAX_SOURCE_BYTES, save_source


class FakeUpload:
    """Minimal UploadFile stand-in: size attribute plus chunked async reads."""

    def __init__(self, chunks: list[bytes], filename: str = "book.txt", size: int | None = None):
        self._chunks = chunks
        self.filename = filename
        self.size = size if size is not None else sum(len(chunk) for chunk in chunks)

    async def read(self, _: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def _project_files(tmp_path) -> list[str]:
    project = tmp_path / "pid1"
    return os.listdir(project) if project.is_dir() else []


def test_oversized_declared_size_rejected_before_writing(tmp_path):
    upload = FakeUpload([], size=MAX_SOURCE_BYTES + 1)
    with pytest.raises(ApiError) as caught:
        asyncio.run(save_source("pid1", upload))
    assert caught.value.status == 413
    assert caught.value.code == SOURCE_TOO_LARGE
    assert _project_files(tmp_path) == []


def test_oversized_stream_rejected_during_write(tmp_path):
    block = b"x" * (1024 * 1024)
    upload = FakeUpload([block] * (MAX_SOURCE_BYTES // len(block) + 2), size=None)
    with pytest.raises(ApiError) as caught:
        asyncio.run(save_source("pid1", upload))
    assert caught.value.code == SOURCE_TOO_LARGE
    assert _project_files(tmp_path) == []


def test_normal_upload_succeeds_with_fields(tmp_path):
    upload = FakeUpload([b"chapter one\n\nbody.\n"], filename="My Book.txt")
    result = asyncio.run(save_source("pid1", upload))
    assert result.fmt == "text"
    assert result.filename == "My Book.txt"
    assert result.path.name.startswith("source-")
    assert len(_project_files(tmp_path)) == 1
    # Web keeps its existing DATA_DIR-relative identity.
    assert result.fields()["source_path"] == os.path.relpath(result.path, tmp_path)


def test_empty_file_rejected_and_cleaned(tmp_path):
    upload = FakeUpload([], filename="empty.txt")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(save_source("pid1", upload))
    assert caught.value.status_code == 422
    assert _project_files(tmp_path) == []


def test_unknown_suffix_rejected(tmp_path):
    upload = FakeUpload([b"data"], filename="archive.zip")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(save_source("pid1", upload))
    assert caught.value.status_code == 422
