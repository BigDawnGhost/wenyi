"""EPUB assembly public API."""

from trans_novel.assemble.epub.metadata import epub_language, translated_toc_title
from trans_novel.assemble.epub.publication import (
    EpubOutput,
    EpubPublishError,
    EpubVerificationError,
    publish_epub,
    publish_epubs,
)
from trans_novel.assemble.epub.verification import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_MEMBERS,
    MAX_MEMBER_BYTES,
    validate_epub,
    validate_epub_triplet,
    verify_epub,
)

__all__ = [
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_MEMBER_BYTES",
    "EpubOutput",
    "EpubPublishError",
    "EpubVerificationError",
    "epub_language",
    "publish_epub",
    "publish_epubs",
    "translated_toc_title",
    "validate_epub",
    "validate_epub_triplet",
    "verify_epub",
]
