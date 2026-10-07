"""Model-free source parsing and validated source artifacts.

Callers own the book run lock while parsing or publishing artifacts. A parsed
document is not an initialized translation and never changes derived state.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from ..config import Config
from ..events import ProgressFn
from ..ingest.epub_reader import peek_epub_title
from ..ingest.fb2_reader import peek_fb2_title
from ..ingest.models import Document
from ..ingest.segmenter import load_document
from ..storage.protocol import ArtifactStorage, Storage
from .runstore import source_sha256, translation_run_dir


def ingest_config(config: Config) -> dict[str, Any]:
    """Fingerprint the parsing inputs shared by preview and initialization."""
    return {
        "source_lang": config.source_lang,
        "target_lang": config.target_lang,
        "max_tokens_per_segment": config.segment.max_tokens_per_segment,
        "pdf_backend": config.pipeline.pdf_backend,
        "babeldoc_bridge_url": config.pipeline.babeldoc_bridge_url,
        "babeldoc_pages": config.pipeline.babeldoc_pages,
        "babeldoc_timeout": config.pipeline.babeldoc_timeout,
    }


def load_parsed_document(
    storage: ArtifactStorage, input_path: str, config: Config, *, actual_sha256: str | None = None
) -> Document | None:
    """Return source chapters only when their content and parsing settings match."""
    cached = storage.read_artifact("parsed_document.json")
    if not isinstance(cached, dict):
        return None
    digest = actual_sha256 or source_sha256(input_path)
    if cached.get("source_sha256") != digest or cached.get("ingest_config") != ingest_config(
        config
    ):
        return None
    document = Document.model_validate(cached["document"])
    document.source_path = input_path
    return document


def parse_document(
    storage: Storage,
    input_path: str,
    config: Config,
    *,
    expected_sha256: str | None = None,
    progress: ProgressFn | None = None,
) -> Document:
    """Parse or reuse source chapters; the caller holds the book run lock."""
    if os.path.splitext(input_path)[1].lower() == ".srt":
        raise ValueError("SRT uses the subtitle workflow. Run wenyi translate <input.srt>.")
    digest = source_sha256(input_path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("Source changed during parsing; ensure the file is stable and retry.")
    if storage.exists():
        storage.ensure_source_identity(input_path, actual_sha256=digest)
    cached = load_parsed_document(storage, input_path, config, actual_sha256=digest)
    if cached is not None:
        document = cached
    else:
        if progress:
            progress(0, 0, "Parsing document…")
        pipeline = config.pipeline
        document = load_document(
            input_path,
            config.source_lang,
            config.target_lang,
            split_segments=config.segment.max_tokens_per_segment,
            cache_dir=storage.source_dir,
            source_hash=digest,
            pdf_backend=pipeline.pdf_backend,
            babeldoc_bridge_url=pipeline.babeldoc_bridge_url,
            babeldoc_pages=pipeline.babeldoc_pages,
            babeldoc_timeout=pipeline.babeldoc_timeout,
        )
    if source_sha256(input_path) != digest:
        raise ValueError("Source changed during parsing; ensure the file is stable and retry.")
    if cached is None:
        storage.write_artifact(
            "parsed_document.json",
            {
                "source_sha256": digest,
                "ingest_config": ingest_config(config),
                "document": document.model_dump(mode="json"),
            },
        )
    return document


def locate_input_storage(
    input_path: str,
    config: Config,
    storage_factory: Callable[[str], Storage],
    *,
    progress: ProgressFn | None = None,
) -> tuple[Storage, str]:
    """Locate local state using filename or metadata titles, without parsing chapters."""
    if progress:
        progress(0, 0, "Locating source…")
    extension = os.path.splitext(input_path)[1].lower()
    if extension == ".srt":
        raise ValueError("SRT uses the subtitle workflow. Run wenyi translate <input.srt>.")
    digest = source_sha256(input_path)
    if extension in (
        ".txt",
        ".text",
        ".md",
        ".markdown",
        ".html",
        ".htm",
        ".xhtml",
        ".docx",
        ".pdf",
    ):
        title = os.path.splitext(os.path.basename(input_path))[0]
    elif extension == ".epub":
        title = peek_epub_title(input_path)
    elif extension == ".fb2":
        title = peek_fb2_title(input_path)
    else:
        raise ValueError(
            f"Unsupported format: {extension} "
            "(supported: .epub / .txt / .md / .fb2 / .html / .xhtml / .pdf / .docx)"
        )
    if source_sha256(input_path) != digest:
        raise ValueError("Source changed during parsing; ensure the file is stable and retry.")
    store = storage_factory(translation_run_dir(config.state_dir, title, config.target_lang))
    return store, digest
