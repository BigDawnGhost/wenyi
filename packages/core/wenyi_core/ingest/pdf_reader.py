"""Read MinerU PDFs through a cached source EPUB without changing the PDF's identity."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from errno import ENOSPC
from pathlib import Path

from ..atomicio import publish_target
from ..models.document import MINERU_EPUB_DIGEST
from .epub_reader import read_epub
from .errors import MinerUError
from .models import Document
from .source_hash import source_sha256


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _source_cache(cache_dir: str, source_hash: str) -> Path:
    if not _valid_digest(source_hash):
        raise ValueError("Invalid source SHA-256 format")
    return Path(cache_dir) / source_hash


def pdf_cache_epub_path(cache_dir: str, source_hash: str) -> Path:
    return _source_cache(cache_dir, source_hash) / "converted.epub"


def cached_epub_source(cache_dir: str, source_hash: str, expected_digest: object) -> str:
    """Resolve the exact derived source used to establish the saved EPUB anchors."""
    cached = pdf_cache_epub_path(cache_dir, source_hash)
    if (
        not _valid_digest(expected_digest)
        or not cached.is_file()
        or source_sha256(cached) != expected_digest
    ):
        raise ValueError(
            "MinerU source EPUB is missing or changed. Restore the original cache or re-import the PDF as a new project."
        )
    return str(cached)


def _resolve_token(api_token: str | None, resolver: Callable[[], str | None] | None) -> str:
    resolution_failed = False
    if resolver is not None:
        try:
            api_token = resolver()
        except Exception:
            resolution_failed = True
    elif api_token is None:
        api_token = os.getenv("MINERU_API_KEY")
    if resolution_failed:
        raise MinerUError("MinerU credential resolution failed") from None
    if not api_token:
        raise MinerUError("API token not provided and MINERU_API_KEY not set") from None
    return api_token


def convert_pdf_to_epub(path: str, output: Path, *, api_token: str, title: str, lang: str) -> None:
    from .mineru_api import extract_pdf
    from .mineru_epub import render_mineru_results

    try:
        with extract_pdf(path, api_token=api_token) as results:
            render_mineru_results(results, output, title=title, lang=lang)
    except OSError as error:
        if error.errno != ENOSPC:
            raise
    else:
        return
    raise MinerUError(
        "PDF conversion ran out of temporary disk space. Free up space on the drive used "
        "for temporary files or the project cache, then retry."
    ) from None


def read_pdf(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    cache_dir: str,
    source_hash: str | None = None,
    api_token: str | None = None,
    mineru_token_resolver: Callable[[], str | None] | None = None,
) -> Document:
    """Parse a cached source EPUB, calling MinerU only when no derived source exists."""
    digest = source_hash or source_sha256(path)
    cache = pdf_cache_epub_path(cache_dir, digest)

    def parse(epub: Path) -> Document:
        derived_digest = source_sha256(epub)
        try:
            doc = read_epub(str(epub), source_lang, target_lang)
        except Exception:
            # Parser and validation errors can include values from the upstream content.
            pass
        else:
            if source_sha256(epub) != derived_digest or source_sha256(path) != digest:
                raise ValueError(
                    "PDF or its source EPUB changed during conversion or parsing. Retry."
                )
            doc.meta[MINERU_EPUB_DIGEST] = derived_digest
            return doc
        raise MinerUError(
            "PDF conversion failed: could not read the MinerU source EPUB. "
            "Check the input PDF or re-import the project."
        ) from None

    if cache.is_file():
        doc = parse(cache)
    else:
        token = _resolve_token(api_token, mineru_token_resolver)
        with publish_target(cache) as temp:
            failed = False
            try:
                convert_pdf_to_epub(
                    path,
                    temp,
                    api_token=token,
                    title=Path(path).stem,
                    lang=source_lang if source_lang not in ("", "auto") else "und",
                )
            # MinerUError is produced locally with diagnostics safe for task events.
            except MinerUError:
                raise
            except Exception:
                failed = True
            # A service exception might include credentials or presigned URLs.
            if failed:
                raise MinerUError(
                    "PDF conversion failed. Check MinerU credentials, service availability, "
                    "and the input PDF, then retry."
                ) from None
            doc = parse(temp)
    doc.fmt = "pdf"
    doc.source_path = os.path.abspath(path)
    return doc
