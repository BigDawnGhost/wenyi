"""PDF reader through MinerU HTML conversion.
Convert PDF to HTML using MinerU Precision API and cache the intermediate file in run state.
Reuse existing HTML for inspection and repeat runs. Parse with html_reader, then restore
fmt="pdf" and the original path. Conversion requires httpx/pypdf and reports installation
guidance if missing; cached HTML needs neither conversion dependency.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from .errors import MinerUError
from .html_reader import read_html
from .models import Document
from .source_hash import source_sha256


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
    mineru_token_resolver: Callable[[], str | None] | None = None,
) -> Document:
    """Convert PDF to HTML and parse it into a Document.
    Cache the intermediate at source/<source_sha256>/converted.html within book state for
    inspection and API-free reuse.
    path is the PDF file; source_lang/target_lang identify languages; cache_dir is the
    preprocessing cache. source_hash may supply a precomputed SHA-256, otherwise the reader
    computes it. api_token defaults to MINERU_API_KEY. An optional resolver overrides
    api_token only on a conversion cache miss; a resolved None means explicitly unavailable
    and never falls back to ambient environment. Return fmt="pdf" with source_path pointing
    to the original PDF.
    """
    digest = source_hash or source_sha256(path)
    html_path = pdf_cache_html_path(cache_dir, digest)
    os.makedirs(os.path.dirname(html_path), exist_ok=True)

    converted = False
    # Call MinerU only when intermediate HTML is absent.
    if not os.path.isfile(html_path):
        _check_deps()
        from .pdf_to_html import convert_pdf_to_html

        resolution_failed = False
        if mineru_token_resolver is not None:
            try:
                api_token = mineru_token_resolver() or ""
            except Exception:
                resolution_failed = True
        elif api_token is None:
            api_token = os.getenv("MINERU_API_KEY")
        # Raise outside exception handlers so even inspected contexts contain no secrets.
        if resolution_failed:
            raise MinerUError("MinerU credential resolution failed") from None
        if not api_token:
            raise MinerUError("API token not provided and MINERU_API_KEY not set") from None
        temporary_html_path = f"{html_path}.tmp"
        try:
            os.remove(temporary_html_path)
        except FileNotFoundError:
            pass
        try:
            convert_pdf_to_html(path, temporary_html_path, api_token=api_token)
            os.replace(temporary_html_path, html_path)
            converted = True
        except Exception:
            shutil.rmtree(os.path.dirname(html_path), ignore_errors=True)
        if not converted:
            # Converter errors can echo credentials, including escaped header bytes.
            # Fixed text is safer than redaction; discard both message and exception chain.
            raise MinerUError(
                "PDF conversion failed. Check MinerU credentials, service availability, "
                "and the input PDF, then retry."
            ) from None

    # Parse intermediate HTML with html_reader.
    doc = read_html(html_path, source_lang, target_lang)
    if source_sha256(path) != digest:
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
