"""Canonical EPUB verification public API."""

from pathlib import Path
from typing import Any

from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError, ThemePlan
from trans_novel.assemble.epub.verification.archive_model import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_MEMBERS,
    MAX_MEMBER_BYTES,
)
from trans_novel.assemble.epub.verification.validation import (
    validate_epub as _validate_epub,
)
from trans_novel.assemble.epub.verification.validation import (
    validate_epub_triplet as _validate_epub_triplet,
)
from trans_novel.assemble.epub.verification.verify import verify_epub


def validate_epub(
    path: Path, *, source_path: Path | None = None, bilingual: bool | None = None
) -> dict[str, Any]:
    return _validate_epub(path, source_path=source_path, bilingual=bilingual)


def validate_epub_triplet(
    source_path: Path,
    mono_path: Path,
    bilingual_path: Path,
    *,
    mono_theme_plan: ThemePlan | None = None,
    bilingual_theme_plan: ThemePlan | None = None,
    store: Any | None = None,
    target_lang: str | None = None,
    bilingual_order: str = "target_first",
) -> dict[str, Any]:
    return _validate_epub_triplet(
        source_path,
        mono_path,
        bilingual_path,
        mono_theme_plan=mono_theme_plan,
        bilingual_theme_plan=bilingual_theme_plan,
        store=store,
        target_lang=target_lang,
        bilingual_order=bilingual_order,
    )


def validate_epub_with_limits(
    path: Path,
    *,
    source_path: Path | None = None,
    bilingual: bool | None = None,
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    return _validate_epub(
        path,
        source_path=source_path,
        bilingual=bilingual,
        max_member_bytes=max_member_bytes,
        max_archive_bytes=max_archive_bytes,
    )


def validate_epub_triplet_with_limits(
    source_path: Path,
    mono_path: Path,
    bilingual_path: Path,
    *,
    mono_theme_plan: ThemePlan | None = None,
    bilingual_theme_plan: ThemePlan | None = None,
    store: Any | None = None,
    target_lang: str | None = None,
    bilingual_order: str = "target_first",
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    return _validate_epub_triplet(
        source_path,
        mono_path,
        bilingual_path,
        mono_theme_plan=mono_theme_plan,
        bilingual_theme_plan=bilingual_theme_plan,
        store=store,
        target_lang=target_lang,
        bilingual_order=bilingual_order,
        max_member_bytes=max_member_bytes,
        max_archive_bytes=max_archive_bytes,
    )


class EpubVerificationError(RuntimeError):
    """A temporary EPUB failed independent post-write verification."""

    def __init__(self, report: dict[str, Any], *, cause: BaseException | None = None):
        self.report = report
        self.published = False
        message = "EPUB verification failed"
        if cause is not None:
            message = f"{message}: {cause}"
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


class EpubPublishError(RuntimeError):
    """Publication or post-publication durability failed."""

    def __init__(
        self, report: dict[str, Any], *, published: bool, cause: BaseException | None = None
    ):
        self.report = report
        self.published = published
        super().__init__("EPUB publication failed")
        if cause is not None:
            self.__cause__ = cause


__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_MEMBER_BYTES",
    "EpubPublishError",
    "EpubVerificationError",
    "ThemeError",
    "ThemePlan",
    "validate_epub",
    "validate_epub_triplet",
    "validate_epub_triplet_with_limits",
    "validate_epub_with_limits",
    "verify_epub",
]
