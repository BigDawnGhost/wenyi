"""Register inspection commands with an invocation context accessor."""

from __future__ import annotations

import typer
from rich.table import Table
from wenyi_core.ingest.errors import IngestError
from wenyi_core.pipeline.runstore import STATUS_DONE
from wenyi_core.timing import load_timing

from wenyi_cli.commands import presentation

from .context import ContextAccessor
from .validation import (
    require_input_file,
    resolve_output_format,
    runstore_for_cli,
    validate_pdf_engine,
)


def register_inspection_commands(app: typer.Typer, context: ContextAccessor) -> None:
    @app.command(rich_help_panel="State and output")
    def languages() -> None:
        """List built-in translation languages (experimental) without calling a model."""
        console = context().console
        from wenyi_core.i18n.languages import profile, supported_languages
        from wenyi_core.i18n.prompts import render

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
        console = context().console
        from wenyi_core.glossary.store import GlossaryStore

        config = context().load_config()
        store = runstore_for_cli(config, input, console=console)
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
        console = context().console
        from wenyi_core.llm.providers.fake import FakeClient
        from wenyi_core.pipeline.orchestrator import Orchestrator

        config = context().load_config()
        require_input_file(input, console=console)
        fmt = resolve_output_format(input, fmt, console=console)
        pdf_engine = validate_pdf_engine(pdf_engine, console=console)
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
        console = context().console
        from wenyi_core.llm.providers.fake import FakeClient
        from wenyi_core.pipeline.orchestrator import Orchestrator

        config = context().load_config()
        require_input_file(input, console=console)
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
