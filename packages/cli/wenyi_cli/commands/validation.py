"""Shared input, backend, format and existing-state validation for CLI commands."""

from __future__ import annotations

import os

import typer
from rich.console import Console
from wenyi_core.config import Config
from wenyi_core.i18n.languages import validate_run_languages
from wenyi_core.ingest.errors import IngestError
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.pipeline.runstore import RunStore, translation_run_dir


def require_input_file(input_path: str, *, console: Console | None = None) -> None:
    """Require an input file, otherwise report the error and exit with status 1."""
    console = console or Console()
    if not os.path.isfile(input_path):
        console.print(f"[red]Input file does not exist: {input_path}[/]")
        raise typer.Exit(1)


def validate_output_format(fmt: str, *, console: Console | None = None) -> str:
    """Normalize and validate the requested output format."""
    console = console or Console()
    normalized = fmt.strip().lower()
    allowed = {"epub", "txt", "html", "markdown", "pdf", "docx"}
    if normalized not in allowed:
        console.print(
            f"[red]Unsupported output format: {fmt} (choose epub / txt / html / markdown / pdf / docx)[/]"
        )
        raise typer.Exit(2)
    return normalized


def resolve_output_format(
    input_path: str, fmt: str | None, *, console: Console | None = None
) -> str | None:
    """Defer PDF defaults to saved backend metadata; keep DOCX and EPUB defaults."""
    console = console or Console()
    if fmt is not None and str(fmt).strip():
        return validate_output_format(str(fmt), console=console)
    if os.path.splitext(input_path)[1].lower() == ".pdf":
        return None
    if os.path.splitext(input_path)[1].lower() == ".docx":
        return "docx"
    return "epub"


def validate_pdf_engine(engine: str, *, console: Console | None = None) -> str:
    """Normalize and validate the PDF rendering engine."""
    console = console or Console()
    normalized = engine.strip().lower()
    if normalized not in {"weasyprint", "fpdf2"}:
        console.print(f"[red]Unsupported PDF engine: {engine} (choose weasyprint / fpdf2)[/]")
        raise typer.Exit(2)
    return normalized


def runstore_for(config: Config, input_path: str, *, console: Console | None = None) -> RunStore:
    """Resolve the title and locate existing state without creating a directory."""
    console = console or Console()
    require_input_file(input_path, console=console)
    if os.path.splitext(input_path)[1].lower() == ".pdf":
        title = os.path.splitext(os.path.basename(input_path))[0]
        run_dir = translation_run_dir(config.state_dir, title, config.target_lang)
        store = RunStore(run_dir, create=False)
    else:
        doc = load_document(input_path, config.source_lang, config.target_lang)
        run_dir = translation_run_dir(config.state_dir, doc.title, config.target_lang)
        store = RunStore(run_dir, create=False)
    if store.exists():
        with store.lock():
            validate_run_languages(store.load_manifest(), config.source_lang, config.target_lang)
            store.ensure_source_identity(input_path)
    return store


def runstore_for_cli(
    config: Config, input_path: str, *, console: Console | None = None
) -> RunStore:
    """Locate state for inspection commands and report identity errors concisely."""
    console = console or Console()
    try:
        return runstore_for(config, input_path, console=console)
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None
