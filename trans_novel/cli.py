"""Command-line entry point using Typer and Rich.

``translate`` runs the complete workflow and resumes interrupted runs.
``prepare``, ``review``, ``report`` and ``assemble`` expose individual stages.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from importlib.metadata import version as package_version
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.progress import Progress
from rich.table import Table
from typer.core import TyperGroup

from .commands import presentation
from .commands import progress as progress_view
from .config import Config
from .i18n.languages import validate_run_languages
from .ingest.errors import IngestError
from .ingest.segmenter import load_document
from .model_commands import register_model_commands
from .pipeline.runstore import STATUS_DONE, RunStore, translation_run_dir
from .timing import load_timing


def _configure_windows_console(
    streams: tuple[object, ...] | None = None,
    *,
    is_windows: bool | None = None,
) -> None:
    """Enable Unicode output on Windows, including PyInstaller one-file executables."""
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return
    for stream in streams or (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


_configure_windows_console()

_CONFIG: dict[str, Any] = {"path": "config.yaml", "skip_api_check": False}


def _config_path_from_args(args: Sequence[str]) -> str:
    """Locate the config before Click parses arguments, including help and early exits."""
    for index, arg in enumerate(args):
        if arg in {"--config", "-c"}:
            if index + 1 < len(args):
                return args[index + 1]
            break
        if arg.startswith("--config="):
            return arg.partition("=")[2]
        if arg.startswith("-c") and len(arg) > 2:
            return arg[2:]
    return "config.yaml"


class _ConfigInitializingGroup(TyperGroup):
    """Check the default configuration before Click dispatches or exits early."""

    def main(
        self,
        args: Sequence[str] | None = None,
        *main_args: Any,
        **main_kwargs: Any,
    ) -> Any:
        """Locate and create a missing default config before Click parses the command."""
        cli_args = list(args) if args is not None else sys.argv[1:]
        config_path = _config_path_from_args(cli_args)
        _CONFIG["path"] = config_path
        _CONFIG["skip_api_check"] = any(arg in {"--help", "-h"} for arg in cli_args)
        Config.create_default_file(config_path)
        from .llm.limits import RequestStopped

        try:
            return super().main(*main_args, args=args, **main_kwargs)
        except RequestStopped as error:
            typer.echo(f"Stopped: {error}", err=True)
            raise SystemExit(1) from None


app = typer.Typer(
    cls=_ConfigInitializingGroup,
    add_completion=False,
    no_args_is_help=True,
    help="Multilingual translation workflows for long-form fiction.",
)
glossary_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Inspect glossary entries, check conflicts and resolve translations.",
)
console = Console()


def _version_callback(value: bool) -> None:
    """Print the installed package version derived from Git tags and exit."""
    if value:
        console.print(package_version("trans-novel"))
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    config: str = typer.Option(
        "config.yaml",
        "--config",
        "-c",
        help="Configuration path; created automatically if missing",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit",
    ),
):
    """Record the config path; workflow commands validate credentials after their overrides."""
    del version
    _CONFIG["path"] = config


def _load_config() -> Config:
    """Load the configuration selected for this invocation."""
    try:
        return Config.load(str(_CONFIG["path"]))
    except (OSError, ValueError, yaml.YAMLError) as error:
        console.print(f"[red]Configuration error: {error}[/]")
        raise typer.Exit(1) from None


def _validate_api_configuration(config: Config, workflow: str) -> None:
    """Validate only the connections reachable after command-line overrides."""
    from .llm.factory import build_client
    from .llm.operations import configured_operations

    if not _CONFIG.get("skip_api_check"):
        try:
            build_client(config).validate_credentials(configured_operations(config, workflow))
        except (ValueError, RuntimeError) as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None


register_model_commands(app, _load_config, console)


def _require_input_file(input_path: str) -> None:
    """Require an input file, otherwise report the error and exit with status 1."""
    if not os.path.isfile(input_path):
        console.print(f"[red]Input file does not exist: {input_path}[/]")
        raise typer.Exit(1)


def _validate_output_format(fmt: str) -> str:
    """Normalize and validate the requested output format."""
    normalized = fmt.strip().lower()
    allowed = {"epub", "txt", "html", "markdown", "pdf", "docx"}
    if normalized not in allowed:
        console.print(
            f"[red]Unsupported output format: {fmt} (choose epub / txt / html / markdown / pdf / docx)[/]"
        )
        raise typer.Exit(2)
    return normalized


def _resolve_output_format(input_path: str, fmt: str | None) -> str | None:
    """Defer PDF defaults to saved backend metadata; keep DOCX and EPUB defaults."""
    if fmt is not None and str(fmt).strip():
        return _validate_output_format(str(fmt))
    if os.path.splitext(input_path)[1].lower() == ".pdf":
        return None
    if os.path.splitext(input_path)[1].lower() == ".docx":
        return "docx"
    return "epub"


def _validate_pdf_engine(engine: str) -> str:
    """Normalize and validate the PDF rendering engine."""
    normalized = engine.strip().lower()
    if normalized not in {"weasyprint", "fpdf2"}:
        console.print(f"[red]Unsupported PDF engine: {engine} (choose weasyprint / fpdf2)[/]")
        raise typer.Exit(2)
    return normalized


def _runstore_for(config: Config, input_path: str) -> RunStore:
    """Resolve the title and locate existing state without creating a directory."""
    _require_input_file(input_path)
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


def _runstore_for_cli(config: Config, input_path: str) -> RunStore:
    """Locate state for inspection commands and report identity errors concisely."""
    try:
        return _runstore_for(config, input_path)
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None


def _translate_impl(
    input_path: str,
    *,
    chapter: int | None = None,
    fmt: str | None = None,
    out: str | None = None,
    pdf_engine: str = "weasyprint",
    polish: bool | None = None,
    review: bool | None = None,
    mono: bool | None = None,
    bilingual: bool | None = None,
) -> None:
    """Run translation and report expected input or configuration errors concisely."""
    try:
        _translate_impl_or_raise(
            input_path,
            chapter=chapter,
            fmt=fmt,
            out=out,
            pdf_engine=pdf_engine,
            polish=polish,
            review=review,
            mono=mono,
            bilingual=bilingual,
        )
    except typer.Exit:
        raise
    except (IngestError, ImportError, OSError, ValueError, RuntimeError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None


def _translate_srt_or_raise(
    input_path: str,
    *,
    chapter: int | None = None,
    fmt: str = "epub",
    out: str | None = None,
    polish: bool | None = None,
    review: bool | None = None,
    mono: bool | None = None,
    bilingual: bool | None = None,
) -> None:
    """Translate subtitles with the strong tier, concurrency and state under state/srt/."""
    from .srt.translate import translate_srt

    if chapter is not None:
        raise ValueError("SRT translation does not support --chapter")
    ignored: list[str] = []
    if fmt != "epub":
        ignored.append("--format")
    if polish is not None:
        ignored.append("--polish/--no-polish")
    if review is not None:
        ignored.append("--review/--no-review")
    if ignored:
        raise ValueError("SRT translation does not support: " + ", ".join(ignored))

    config = _load_config()
    _validate_api_configuration(config, "srt")
    _require_input_file(input_path)
    if mono is not None:
        config.output.mono = mono
    if bilingual is not None:
        config.output.bilingual = bilingual

    with Progress(
        *progress_view.progress_columns(),
        console=console,
    ) as prog:
        cb = progress_view.RichProgressBridge(prog, "Translating subtitles…")
        result = translate_srt(
            input_path,
            config,
            out=out,
            mono=mono,
            bilingual=bilingual,
            progress=cb,
        )

    presentation.print_subtitle_summary(
        console,
        translated=result["translated"],
        cue_count=result["cue_count"],
        run_dir=result["run_dir"],
    )
    presentation.print_usage(console, result.get("usage") or {})
    presentation.print_timing(console, load_timing(result["run_dir"]))
    for path in result.get("outputs") or []:
        console.print(f"Translation: [bold]{path}[/]")


def _translate_impl_or_raise(
    input_path: str,
    *,
    chapter: int | None = None,
    fmt: str | None = None,
    out: str | None = None,
    pdf_engine: str = "weasyprint",
    polish: bool | None = None,
    review: bool | None = None,
    mono: bool | None = None,
    bilingual: bool | None = None,
) -> None:
    """Run translation; let ``_translate_impl`` convert exceptions to CLI errors."""
    from .pipeline.orchestrator import Orchestrator

    if os.path.splitext(input_path)[1].lower() == ".srt":
        _translate_srt_or_raise(
            input_path,
            chapter=chapter,
            fmt=fmt or "epub",
            out=out,
            polish=polish,
            review=review,
            mono=mono,
            bilingual=bilingual,
        )
        return

    fmt = _resolve_output_format(input_path, fmt)
    pdf_engine = _validate_pdf_engine(pdf_engine)
    config = _load_config()
    if polish is not None:
        config.pipeline.polish = polish
    if review is not None:
        config.pipeline.review = review
    if mono is not None:
        config.output.mono = mono
    if bilingual is not None:
        config.output.bilingual = bilingual
    if chapter is not None:
        ignored: list[str] = []
        if fmt not in {None, "epub"}:
            ignored.append("--format")
        if out is not None:
            ignored.append("--out")
        if review is not None:
            ignored.append("--review/--no-review")
        if mono is not None:
            ignored.append("--mono/--no-mono")
        if bilingual is not None:
            ignored.append("--bilingual/--no-bilingual")
        if ignored:
            raise ValueError(
                "--chapter only translates and saves the selected chapter; incompatible finalization options: "
                + ", ".join(ignored)
            )

    if chapter is not None:
        config.pipeline.review = False
    _validate_api_configuration(config, "translate")
    _require_input_file(input_path)
    orch = Orchestrator(config)

    with (
        orch.client.interrupt_scope(),
        Progress(
            *progress_view.progress_columns(),
            console=console,
        ) as prog,
    ):
        cb = progress_view.RichProgressBridge(prog, "Preparing…")

        if chapter is not None:
            try:
                store = orch.run(input_path, only_chapter=chapter, progress=cb)
            except ValueError as error:
                console.print(f"[red]{error}[/]")
                raise typer.Exit(2) from error
            console.print(
                f"[green]Translated chapter {chapter}[/], State directory: {store.run_dir}"
            )
            presentation.print_usage(console, store.load_usage() or {})
            presentation.print_timing(console, load_timing(store.run_dir))
            return

        result = orch.run_all(
            input_path,
            progress=cb,
            out_format=fmt,
            out_path=out,
            pdf_engine=pdf_engine,
        )

    presentation.print_translation_summary(console, result["report"]["summary"])
    presentation.print_usage(console, result["store"].load_usage() or {})
    presentation.print_timing(console, load_timing(result["store"].run_dir))
    for path in result.get("outputs") or [result["output"]]:
        console.print(f"Translation: [bold]{path}[/]")
    if result.get("review_dir"):
        presentation.print_review_details(
            console, result.get("review_result"), result["review_dir"]
        )


def _prepare_impl(input_path: str) -> None:
    """Complete preparation without translating body text or exporting files."""
    from .pipeline.orchestrator import Orchestrator

    try:
        config = _load_config()
        _validate_api_configuration(config, "prepare")
        _require_input_file(input_path)
        orch = Orchestrator(config)
        with (
            orch.client.interrupt_scope(),
            Progress(
                *progress_view.progress_columns(),
                console=console,
            ) as prog,
        ):
            cb = progress_view.RichProgressBridge(prog, "Preparing…")
            store = orch.prepare_for_translation(input_path, progress=cb)
    except typer.Exit:
        raise
    except (IngestError, ImportError, OSError, ValueError, RuntimeError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None

    manifest = store.load_manifest()
    chapters = manifest.get("chapters", [])
    analysis = store.load_analysis() or {}
    digests = sum(
        bool(store.load_chapter(item["index"]).meta.get("source_digest")) for item in chapters
    )
    presentation.print_preparation_summary(
        console,
        chapter_count=len(chapters),
        digest_count=digests,
        has_synopsis=bool(analysis.get("book_synopsis")),
        run_dir=store.run_dir,
    )
    presentation.print_usage(console, store.load_usage() or {})
    presentation.print_timing(console, load_timing(store.run_dir))


# ── Complete workflow / Preparation ────────────────────────────────────────────────
@app.command(rich_help_panel="Main workflow")
def translate(
    input: str = typer.Argument(
        ...,
        help="Book or subtitles to translate (EPUB / FB2 / TXT / Markdown / HTML / PDF / DOCX / SRT)",
    ),
    chapter: int | None = typer.Option(
        None,
        "--chapter",
        min=0,
        help="Translate and save one chapter (zero-based); skip review, report and export",
    ),
    fmt: str | None = typer.Option(
        None,
        "--format",
        help="Output format: epub / txt / html / markdown / pdf / docx; default: pdf for BabelDOC PDF state, docx for .docx input, epub otherwise",
    ),
    out: str | None = typer.Option(
        None,
        "--out",
        help="Monolingual output path; defaults to the output directory beside the source",
    ),
    pdf_engine: str = typer.Option(
        "weasyprint",
        "--pdf-engine",
        help="PDF renderer: weasyprint (default) / fpdf2",
    ),
    polish: bool | None = typer.Option(
        None,
        "--polish/--no-polish",
        help="Override pipeline.polish to enable or disable polishing",
    ),
    review: bool | None = typer.Option(
        None,
        "--review/--no-review",
        help="Override pipeline.review to enable or disable final whole-book review",
    ),
    mono: bool | None = typer.Option(
        None,
        "--mono/--no-mono",
        help="Override output.mono to enable or disable monolingual output",
    ),
    bilingual: bool | None = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help="Override output.bilingual to enable or disable bilingual output",
    ),
):
    """Prepare, translate, optionally review, report and export. Repeat to resume."""
    _translate_impl(
        input,
        chapter=chapter,
        fmt=fmt,
        out=out,
        pdf_engine=pdf_engine,
        polish=polish,
        review=review,
        mono=mono,
        bilingual=bilingual,
    )


@app.command(rich_help_panel="Main workflow")
def prepare(
    input: str = typer.Argument(
        ...,
        help="Book to prepare (EPUB / FB2 / TXT / Markdown / HTML / PDF / DOCX)",
    ),
) -> None:
    """Parse, detect language, analyze style and terms, and prescan the book."""
    _prepare_impl(input)


@app.command(rich_help_panel="Quality checks")
def review(
    input: str = typer.Argument(..., help="Source file whose entire body has been translated"),
    autofix: bool | None = typer.Option(
        None,
        "--autofix/--no-autofix",
        help="Override pipeline.review_autofix to publish revisions to formal chapters",
    ),
):
    """Run evidence review, shadow revisions and blind rechecks, with optional autofix."""
    from .pipeline.orchestrator import Orchestrator

    try:
        config = _load_config()
        if autofix is not None:
            config.pipeline.review_autofix = autofix
        _validate_api_configuration(config, "review")
        _require_input_file(input)
        orch = Orchestrator(config)

        with (
            orch.client.interrupt_scope(),
            Progress(
                *progress_view.progress_columns(),
                console=console,
            ) as prog,
        ):
            cb = progress_view.RichProgressBridge(prog, "Preparing whole-book review…")
            result = orch.run_review(input, progress=cb)
    except typer.Exit:
        raise
    except (IngestError, ImportError, OSError, ValueError, RuntimeError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None

    presentation.print_review_summary(console, result["review_result"], result["review_dir"])
    presentation.print_timing(console, load_timing(result["store"].run_dir))


# ── Inspection / Individual stages ──────────────────────────────────────────────────────
@app.command(rich_help_panel="State and output")
def languages() -> None:
    """List built-in translation languages (experimental) without calling a model."""
    from .i18n.languages import profile, supported_languages
    from .i18n.prompts import render

    table = Table("Code", "Language")
    for code in supported_languages():
        entry = profile(code)
        render("translator_system", src="auto", tgt=code)
        table.add_row(code, entry["english_name"])
    console.print(table)
    console.print("Set language.source and language.target; source also accepts auto.")


@app.command(rich_help_panel="State and output")
def status(
    input: str = typer.Argument(..., help="Source file with existing translation state"),
) -> None:
    """Show chapter progress and glossary statistics."""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for_cli(config, input)
    if not store.exists():
        console.print("[yellow]No progress found. Run prepare or translate first.[/]")
        raise typer.Exit(1)
    m = store.load_manifest()
    console.print(f"“{m['title']}”({m['fmt']})  {m['source_lang']}→{m['target_lang']}")
    table = Table("", "#", "Chapter", "Translation")
    for c in m["chapters"]:
        mark = "✓" if c["status"] == STATUS_DONE else "·"
        table.add_row(
            mark,
            str(c["index"]),
            c["title"],
            c["status"],
        )
    console.print(table)
    g = GlossaryStore(store.glossary_path)
    console.print("Glossary: ", g.stats())
    g.close()
    presentation.print_timing(console, load_timing(store.run_dir))


@glossary_app.command("list")
def glossary_list(
    input: str = typer.Argument(..., help="Source file with existing translation state"),
) -> None:
    """List established glossary translations and their status."""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for_cli(config, input)
    if not store.exists():
        console.print("[yellow]No progress found. Run prepare or translate first.[/]")
        raise typer.Exit(1)
    g = GlossaryStore(store.glossary_path)
    try:
        table = Table("Source", "Translation", "Type", "Status")
        # all_terms() preserves insertion order for prompt prefix caching.
        # Sort only this display by type/source; preserve the shared data order.
        for term in sorted(g.all_terms(), key=lambda t: (t.type, t.source)):
            table.add_row(
                term.source,
                term.target,
                f"{term.type}{'/' + term.gender if term.gender else ''}",
                term.status,
            )
        console.print(table)
    finally:
        g.close()


@glossary_app.command("conflicts")
def glossary_conflicts(
    input: str = typer.Argument(..., help="Source file with existing translation state"),
) -> None:
    """List unresolved translation conflicts discovered during extraction."""
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for_cli(config, input)
    if not store.exists():
        console.print("[yellow]No progress found. Run translate or prepare first.[/]")
        raise typer.Exit(1)
    glossary = GlossaryStore(store.glossary_path)
    try:
        conflicts = glossary.open_conflicts()
        if not conflicts:
            console.print("No unresolved glossary conflicts.")
            return
        for conflict in conflicts:
            console.print(
                f"  {conflict['source']}: existing “{conflict['existing_target']}” vs "
                f"proposed “{conflict['proposed_target']}”"
                f"(chapter {conflict['chapter']})"
            )
    finally:
        glossary.close()


@glossary_app.command("resolve")
def glossary_resolve(
    input: str = typer.Argument(..., help="Source file with existing translation state"),
    source: str = typer.Argument(..., help="Source term to resolve"),
    target: str = typer.Argument(..., help="Target translation to use consistently from now on"),
) -> None:
    """Resolve an existing term to a chosen translation and close its conflicts."""
    from .glossary import resolver
    from .glossary.store import GlossaryStore

    config = _load_config()
    store = _runstore_for_cli(config, input)
    if not store.exists():
        console.print("[yellow]No progress found. Run translate or prepare first.[/]")
        raise typer.Exit(1)
    glossary = GlossaryStore(store.glossary_path)
    try:
        if not resolver.resolve(glossary, source, target):
            console.print(f"[red]Term does not exist: {source}[/]")
            raise typer.Exit(1)
        console.print(f"Resolved {source} → {target}")
    finally:
        glossary.close()


@app.command(rich_help_panel="State and output")
def assemble(
    input: str = typer.Argument(..., help="Source file with a complete or partial translation"),
    out: str | None = typer.Option(
        None,
        "--out",
        help="Monolingual output path; defaults to the output directory beside the source",
    ),
    fmt: str | None = typer.Option(
        None,
        "--format",
        help="Output format: epub / txt / html / markdown / pdf / docx; default: pdf for BabelDOC PDF state, docx for .docx input, epub otherwise",
    ),
    pdf_engine: str = typer.Option(
        "weasyprint",
        "--pdf-engine",
        help="PDF renderer: weasyprint (default) / fpdf2",
    ),
    mono: bool | None = typer.Option(
        None,
        "--mono/--no-mono",
        help="Override output.mono to enable or disable monolingual output",
    ),
    bilingual: bool | None = typer.Option(
        None,
        "--bilingual/--no-bilingual",
        help="Override output.bilingual to enable or disable bilingual output",
    ),
):
    """Export translations from existing state without calling a model."""
    from .llm.providers.fake import FakeClient
    from .pipeline.orchestrator import Orchestrator

    config = _load_config()
    _require_input_file(input)
    fmt = _resolve_output_format(input, fmt)
    pdf_engine = _validate_pdf_engine(pdf_engine)
    if mono is not None:
        config.output.mono = mono
    if bilingual is not None:
        config.output.bilingual = bilingual
    try:
        result = Orchestrator(config, client=FakeClient()).run_assemble(
            input,
            out_format=fmt,
            out_path=out,
            pdf_engine=pdf_engine,
        )
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None
    paths = result["outputs"]
    presentation.print_timing(console, load_timing(result["store"].run_dir))
    for path in paths:
        console.print(f"Translation written: [bold]{path}[/]")


@app.command(rich_help_panel="State and output")
def report(
    input: str = typer.Argument(..., help="Source file with existing translation state"),
) -> None:
    """Regenerate report.json from chapter and glossary state without calling a model."""
    from .llm.providers.fake import FakeClient
    from .pipeline.orchestrator import Orchestrator

    config = _load_config()
    _require_input_file(input)
    try:
        result = Orchestrator(config, client=FakeClient()).run_report(input)
    except (IngestError, OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None
    store = result["store"]
    rep = result["report"]
    s = rep["summary"]
    console.print(f"Report written to {store.report_path}")
    console.print(
        f"  Chapter {s['chapters_done']}/{s['chapters_total']}  Terms {s['terms']}  "
        f"Unresolved conflicts {s['open_conflicts']}  Empty translations {s['empty_targets']}"
    )


app.add_typer(glossary_app, name="glossary", rich_help_panel="Glossary")


def main() -> None:
    """Start the Typer command-line application."""
    app()


if __name__ == "__main__":
    main()
