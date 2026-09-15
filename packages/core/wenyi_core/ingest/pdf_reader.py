"""PDF reader through MinerU HTML conversion.
Convert PDF to HTML using MinerU Precision API and cache the intermediate file in run state.
Reuse existing HTML for inspection and repeat runs. Parse with html_reader, then restore
fmt="pdf" and the original path. Conversion requires httpx/pypdf and reports installation
guidance if missing; cached HTML needs neither conversion dependency.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

from .errors import MinerUError
from .html_reader import read_html
from .models import Document


def _source_sha256(path: str) -> str:
    """Stream the PDF content hash to bind caches for direct reader calls."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_cache_html_path(cache_dir: str, source_hash: str) -> str:
    """Return the MinerU HTML cache path isolated by source hash."""
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("Invalid source SHA-256 format")
    return os.path.join(cache_dir, source_hash, "converted.html")


def _check_deps() -> None:
    """Check optional PDF conversion dependencies and report installation guidance if absent."""
    missing = []
    for mod, pkg in [("httpx", "httpx"), ("pypdf", "pypdf")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise ImportError(
            f"PDF conversion requires optional dependencies; run: \n"
            f"  uv pip install {' '.join(missing)}\n"
            f"After installing dependencies and converting once, inspect the cached source/<SHA-256>/"
            f"converted.html。"
        )


def read_pdf(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    cache_dir: str,
    source_hash: str | None = None,
    api_token: str | None = None,
) -> Document:
    """Convert PDF to HTML and parse it into a Document.
    Cache the intermediate at source/<source_sha256>/converted.html within book state for
    inspection and API-free reuse.
    path is the PDF file; source_lang/target_lang identify languages; cache_dir is the
    preprocessing cache. source_hash may supply a precomputed SHA-256, otherwise the reader
    computes it. api_token defaults to MINERU_API_KEY. Return fmt="pdf" with source_path
    pointing to the original PDF.
    """
    digest = source_hash or _source_sha256(path)
    html_path = pdf_cache_html_path(cache_dir, digest)
    os.makedirs(os.path.dirname(html_path), exist_ok=True)

    converted = False
    # Call MinerU only when intermediate HTML is absent.
    if not os.path.isfile(html_path):
        _check_deps()
        from .pdf_to_html import convert_pdf_to_html

        temporary_html_path = f"{html_path}.tmp"
        try:
            os.remove(temporary_html_path)
        except FileNotFoundError:
            pass
        try:
            convert_pdf_to_html(path, temporary_html_path, api_token=api_token)
            os.replace(temporary_html_path, html_path)
            converted = True
        except MinerUError:
            shutil.rmtree(os.path.dirname(html_path), ignore_errors=True)
            raise
        except Exception as error:
            shutil.rmtree(os.path.dirname(html_path), ignore_errors=True)
            # Wrap HTTP, PDF parsing, ZIP extraction and filesystem failures as input-layer errors.
            # Retain the original exception as the cause for debugging.
            raise MinerUError(f"PDF conversion failed: {error}") from error

    # Parse intermediate HTML with html_reader.
    doc = read_html(html_path, source_lang, target_lang)
    if _source_sha256(path) != digest:
        if converted:
            shutil.rmtree(os.path.dirname(html_path), ignore_errors=True)
        raise ValueError(
            "PDF changed during conversion or parsing; the new cache was discarded. Retry."
        )

    # Restore original PDF metadata.
    doc.title = Path(path).stem
    doc.fmt = "pdf"
    doc.source_path = os.path.abspath(path)

    return doc
