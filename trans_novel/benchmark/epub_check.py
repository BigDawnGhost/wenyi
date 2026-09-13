"""Facade for canonical production EPUB verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trans_novel.assemble.epub.verification import (
    MAX_ARCHIVE_BYTES,
    MAX_MEMBER_BYTES,
    archive_model,
)
from trans_novel.assemble.epub.verification import (
    validate_epub_with_limits as _validate_epub,
)
from trans_novel.assemble.epub.verification.verify import output_label

_MAX_MEMBER_BYTES = MAX_MEMBER_BYTES
_MAX_ARCHIVE_BYTES = MAX_ARCHIVE_BYTES


def validate_epub(path, *, source_path=None, bilingual=None):
    return _validate_epub(
        path,
        source_path=source_path,
        bilingual=bilingual,
        max_member_bytes=_MAX_MEMBER_BYTES,
        max_archive_bytes=_MAX_ARCHIVE_BYTES,
    )


def validate_epub_triplet(
    source_path,
    mono_path,
    bilingual_path,
    *,
    publication_report: dict[str, Any] | None,
    output_digest: str | None,
) -> dict[str, Any]:
    """核对源书及两份输出的验收记录与当前文件，不凭单文件验证结果推断对齐。"""
    mismatch = "publication proof mismatch"
    if (
        not isinstance(publication_report, dict)
        or not isinstance(output_digest, str)
        or not output_digest
        or publication_report.get("output_digest") != output_digest
        or publication_report.get("passed") is not True
        or publication_report.get("published") is not True
    ):
        raise ValueError(mismatch)
    triplet = publication_report.get("triplet")
    published = publication_report.get("published_outputs")
    if (
        not isinstance(triplet, dict)
        or triplet.get("schema_version") != 1
        or not isinstance(published, dict)
    ):
        raise ValueError(mismatch)
    paths = {"source": source_path, "mono": mono_path, "bilingual": bilingual_path}
    hashes = {name: archive_model.sha256(Path(path)) for name, path in paths.items()}
    for name, digest in hashes.items():
        part = triplet.get(name)
        if (
            not digest
            or not isinstance(part, dict)
            or part.get("path_sha256") != digest
            or type(part.get("structural_pass")) is not bool
            or not isinstance(part.get("failures"), list)
            or part["structural_pass"] != (not part["failures"])
        ):
            raise ValueError(mismatch)
        if name == "source":
            continue
        receipt = published.get(output_label(paths[name]))
        if (
            not isinstance(receipt, dict)
            or receipt.get("passed") is not True
            or receipt.get("published") is not True
            or receipt.get("output_sha256") != digest
            or receipt.get("source_sha256") != hashes["source"]
            or receipt.get("output_digest") != output_digest
            or receipt.get("triplet") != triplet
        ):
            raise ValueError(mismatch)
    if triplet.get("structural_pass") is not all(
        triplet[name]["structural_pass"] for name in paths
    ):
        raise ValueError(mismatch)
    return triplet


__all__ = ["validate_epub", "validate_epub_triplet"]
