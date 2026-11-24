"""Shared input, backend, format and existing-state validation for CLI commands."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import typer
from rich.console import Console

if TYPE_CHECKING:
    from wenyi_core.config import Config
    from wenyi_core.storage.run_store import RunStore


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
    """Keep HTML/DOCX input formats; defer PDF defaults to saved backend metadata."""
    console = console or Console()
    if fmt is not None and str(fmt).strip():
        return validate_output_format(str(fmt), console=console)
    if os.path.splitext(input_path)[1].lower() == ".pdf":
        return None
    if os.path.splitext(input_path)[1].lower() == ".docx":
        return "docx"
    if os.path.splitext(input_path)[1].lower() in {".html", ".htm", ".xhtml"}:
        return "html"
    return "epub"


def runstore_for(config: Config, input_path: str) -> RunStore:
    """Resolve the title and locate existing state without creating a directory."""
    # Deferred: reading a title pulls the ingest stack (bs4/lxml) in; --help
    # must not pay for that.
    from wenyi_core.pipeline.prepare.input_preparation import locate_input_run
    from wenyi_core.storage.run_store import RunStore

    run_dir, _ = locate_input_run(input_path, config)
    return RunStore(run_dir, create=False)


def runstore_for_cli(
    config: Config, input_path: str, *, console: Console | None = None
) -> RunStore:
    # Deferred with the ingest stack; the base error class drags it in.
    from wenyi_core.models.errors import IngestError

    console = console or Console()
    require_input_file(input_path, console=console)
    try:
        return runstore_for(config, input_path)
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None


def resolve_export_out_path(
    out_path: str | None,
    *,
    source_path: str,
    bilingual: bool = False,
    overwrite: bool = False,
    console: Console | None = None,
) -> str | None:
    """Keep an existing ``--out`` target by exporting to a free numbered name instead.

    Unless ``overwrite`` is set, an existing output file and its bilingual companion
    are never replaced: the export moves to the next free ``name (n).ext`` path and
    the console explains the change. A path equal to the source is returned unchanged
    so the core source-protection error still fires.
    """
    if out_path is None or overwrite:
        return out_path
    if os.path.realpath(out_path) == os.path.realpath(source_path):
        return out_path

    def occupied(path: str) -> bool:
        if os.path.exists(path):
            return True
        if not bilingual:
            return False
        from wenyi_core.assemble.writer_common import bilingual_out_path

        return os.path.exists(bilingual_out_path(path))

    if not occupied(out_path):
        return out_path
    base, ext = os.path.splitext(out_path)
    index = 1
    candidate = f"{base} ({index}){ext}"
    while occupied(candidate):
        index += 1
        candidate = f"{base} ({index}){ext}"
    console = console or Console()
    console.print(
        f"[yellow]Output exists: {out_path}; writing to {candidate} instead"
        " (pass --overwrite to replace it)[/]"
    )
    return candidate
