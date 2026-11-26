"""Model-free source parsing and validated source artifacts.

Callers own the book run lock while parsing or publishing artifacts. A parsed
document is not an initialized translation and never changes derived state.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from ...assemble.writer_common import validate_pdf_source_format
from ...config import Config
from ...events import ProgressFn
from ...ingest.boilerplate import BUILTIN_RULES_VERSION, mark_boilerplate_segments
from ...ingest.epub_reader import peek_epub_title
from ...ingest.fb2_reader import peek_fb2_title
from ...ingest.pdf_reader import cached_epub_source
from ...ingest.segmenter import load_document
from ...markup.objects import OBJECT_SCHEMA_VERSION
from ...models.document import MINERU_EPUB_DIGEST, Document, uses_epub_template
from ...storage.protocol import Storage
from ...storage.run_store import resolve_file_run_dir, source_sha256


def ingest_config(config: Config) -> dict[str, Any]:
    """Fingerprint the parsing inputs shared by preview and initialization."""
    data: dict[str, Any] = {
        "source_lang": config.source_lang,
        "target_lang": config.target_lang,
        "max_tokens_per_segment": config.segment.max_tokens_per_segment,
        "pdf_backend": config.pipeline.pdf_backend,
        "babeldoc_bridge_url": config.pipeline.babeldoc_bridge_url,
        "babeldoc_pages": config.pipeline.babeldoc_pages,
        "babeldoc_timeout": config.pipeline.babeldoc_timeout,
    }
    if config.pipeline.skip_boilerplate:
        data["skip_boilerplate"] = True
        data["boilerplate_rules_version"] = BUILTIN_RULES_VERSION
        data["boilerplate_patterns"] = list(config.pipeline.boilerplate_patterns)
    return data


# Matches "epub_schema" written by the native EPUB reader (rust/src/epub/ingest.rs).
EPUB_INGEST_SCHEMA = 7


def load_parsed_document(
    storage: Storage, input_path: str, config: Config, *, actual_sha256: str | None = None
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
    if (
        os.path.splitext(input_path)[1].lower() == ".pdf"
        and config.pipeline.pdf_backend == "mineru"
    ):
        if MINERU_EPUB_DIGEST not in document.meta:
            return None
        cached_epub_source(storage.source_dir, digest, document.meta[MINERU_EPUB_DIGEST])
        document.fmt = "pdf"
    if (
        uses_epub_template(document.fmt, document.meta)
        and document.meta.get("epub_objects") != OBJECT_SCHEMA_VERSION
    ):
        return None
    document.source_path = input_path
    return document


def parse_document(
    storage: Storage,
    input_path: str,
    config: Config,
    *,
    expected_sha256: str | None = None,
    progress: ProgressFn | None = None,
    mineru_token_resolver: Callable[[], str | None] | None = None,
) -> Document:
    """Parse or reuse source chapters; the caller holds the book run lock."""
    digest = source_sha256(input_path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("Source changed during parsing; ensure the file is stable and retry.")
    if storage.exists():
        storage.ensure_source_identity(input_path, actual_sha256=digest)
        manifest = storage.load_manifest()
        validate_pdf_source_format(manifest)
        if os.path.splitext(input_path)[1].lower() == ".pdf":
            meta = manifest.get("meta") or {}
            saved_mineru = MINERU_EPUB_DIGEST in meta
            if saved_mineru != (config.pipeline.pdf_backend == "mineru"):
                raise ValueError(
                    "Changing the PDF parser requires importing the PDF as a new project."
                )
            if saved_mineru:
                cached_epub_source(storage.source_dir, digest, meta[MINERU_EPUB_DIGEST])
    cached = load_parsed_document(storage, input_path, config, actual_sha256=digest)
    # A cache from an older EPUB reader may miss loose-text body segments or newer package
    # metadata (creators, navigation labels). Read-only views and source-only exports keep
    # honoring the saved epub_schema; a parse run refreshes both EPUB and MinerU caches locally.
    if (
        cached is not None
        and uses_epub_template(cached.fmt, cached.meta)
        and cached.meta.get("epub_schema") != EPUB_INGEST_SCHEMA
    ):
        cached = None
    if cached is not None:
        document = cached
    else:
        if progress:
            progress(0, 0, "progress.stage.parsing")
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
            mineru_token_resolver=mineru_token_resolver,
        )
        mark_boilerplate_segments(
            document,
            enabled=pipeline.skip_boilerplate,
            custom_patterns=pipeline.boilerplate_patterns,
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
    """Resolve local state before constructing a store that may create directories."""
    run_dir, digest = locate_input_run(input_path, config, progress=progress)
    return storage_factory(run_dir), digest


def locate_input_run(
    input_path: str, config: Config, *, progress: ProgressFn | None = None
) -> tuple[str, str]:
    """Locate CLI state using the filename or metadata title, without parsing chapters."""
    if progress:
        progress(0, 0, "progress.stage.locateSource")
    extension = os.path.splitext(input_path)[1].lower()
    peek_title = {".epub": peek_epub_title, ".fb2": peek_fb2_title}.get(extension)
    if peek_title is not None:
        digest = source_sha256(input_path)
        title = peek_title(input_path)
        if source_sha256(input_path) != digest:
            raise ValueError("Source changed during parsing; ensure the file is stable and retry.")
    elif extension in (
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
        digest = source_sha256(input_path)
    else:
        raise ValueError(
            f"Unsupported format: {extension} "
            "(supported: .epub / .txt / .md / .fb2 / .html / .xhtml / .pdf / .docx)"
        )
    run_dir = resolve_file_run_dir(
        config.state_dir,
        title,
        config.target_lang,
        source_lang=config.source_lang,
        actual_sha256=digest,
    )
    return run_dir, digest
