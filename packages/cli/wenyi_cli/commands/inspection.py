"""Register inspection commands with an invocation context accessor."""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table
from wenyi_core.timing import load_timing

from wenyi_cli.commands import presentation

from .context import ContextAccessor
from .run_report import _crashed_step_events, _read_events, build_report
from .validation import (
    require_input_file,
    resolve_export_out_path,
    resolve_output_format,
    runstore_for,
    runstore_for_cli,
)


def _format_failure_event(event: dict[str, Any]) -> str:
    """Render one recorded failure event as a concise, configuration-free message."""
    name = event.get("event")
    if name == "llm_retry_exhausted":
        message = f"LLM request failed after {event.get('attempts', '?')} attempts"
        if event.get("error_type"):
            message += f": {event['error_type']}"
        detail = ", ".join(
            f"{key}={event[key]}"
            for key in ("reason", "stage", "status_code")
            if event.get(key) is not None
        )
        if detail:
            message += f" ({detail})"
        return message
    if name == "pipeline_error":
        error_type = event.get("error_type") or "unknown"
        error = event.get("error") or "no error message recorded"
        return f"{error_type}: {error}"
    steps = [step for step in event.get("steps") or [] if isinstance(step, str)]
    if steps:
        return f"Run failed: pipeline step(s) started but never finished: {', '.join(steps)}"
    return "Run failed: a pipeline step started but never finished (see the Wenyi log)"


def describe_last_failure(source: str, config_path: str) -> str | None:
    """Return the latest failure reason from a run's event log, or None.

    Machine failure receipts use this so supervisors can read the real reason
    without scraping logs. Reads the same error events as ``inspect_run`` and
    prefers them in the same order. Best-effort and silent: any problem locating
    or reading the state returns None so the caller can show its own fallback.
    """
    try:
        from wenyi_core.config_rebuild import load_rebuilt_config

        store = runstore_for(load_rebuilt_config(config_path), source)
        events = _read_events(store.event_log_path)
        exhausted = [event for event in events if event.get("event") == "llm_retry_exhausted"]
        pipeline_errors = [event for event in events if event.get("event") == "pipeline_error"]
        crashed = _crashed_step_events(events)
    except Exception:
        return None
    failures = pipeline_errors + crashed
    last = exhausted[-1] if exhausted else (failures[-1] if failures else None)
    if last is None:
        return None
    try:
        return _format_failure_event(last)
    except Exception:
        return None


def register_inspection_commands(app: typer.Typer, context: ContextAccessor) -> None:
    @app.command(rich_help_panel="State and output")
    def language_policy(
        source: str | None = typer.Option(
            None, "--source", help="Resolved source language for preview"
        ),
        fmt: str | None = typer.Option(None, "--format", help="Actual export format"),
        backend: str = typer.Option("native", "--backend", help="native / babeldoc"),
    ) -> None:
        """Preview built-in language operations and phase fingerprints without model calls."""
        from wenyi_core.i18n.languages import require_language

        try:
            config = context().load_config()
            if source is not None:
                config.source_lang = require_language(source, allow_auto=True)
            preview = config.language_policy_preview(
                format=fmt or "epub",
                backend=backend,
            )
        except ValueError as error:
            context().console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None
        context().console.print_json(data=preview)

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
            mark = "✓" if c["status"] == "done" else "·"
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
    def inspect_run(
        input: str = typer.Argument(..., help="Source file with existing translation state"),
        verbose: bool = typer.Option(False, "--verbose", help="Include the last failure traceback"),
    ) -> None:
        """Summarize one run: failures, retries, timing and token usage, read-only."""
        console = context().console
        config = context().load_config()
        store = runstore_for_cli(config, input, console=console)
        if not store.exists():
            console.print("[yellow]No progress found. Run prepare or translate first.[/]")
            raise typer.Exit(1)

        report = build_report(store)
        console.print(f"Run: [bold]{report['run_dir']}[/]")
        console.print(
            f"  Events {report['events']}  Failures {report['failures']}  "
            f"Retries {report['retries']}  Pauses {report['pauses']}"
        )
        failure = report["last_failure"]
        if failure:
            # The type is what makes a failure diagnosable; the text alone is not.
            console.print(
                f"  Last failure: [red]{failure['error_type'] or 'unknown'}[/] {failure['error'] or ''}"
            )
            if verbose and failure.get("traceback"):
                console.print(failure["traceback"])
        else:
            console.print("  Last failure: none")

        usage = report["usage"]
        if usage:
            console.print(
                "  Tokens: "
                f"in {usage.get('prompt_tokens', 0)}  out {usage.get('completion_tokens', 0)}  "
                f"cache read {usage.get('cache_hit_tokens', 0)}"
            )
        presentation.print_timing(console, report["timing"])

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
            help="Output format: epub / txt / html / markdown / docx / pdf (BabelDOC only); default: html for HTML input, pdf for BabelDOC PDF state, docx for .docx input, epub otherwise",
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
        overwrite: bool = typer.Option(
            False,
            "--overwrite",
            help="Replace existing --out files instead of writing numbered alternatives",
        ),
    ):
        """Export translations from existing state without calling a model."""
        from wenyi_core.models.errors import ExportPartialError, IngestError

        console = context().console
        from wenyi_core.llm.providers.fake import FakeClient
        from wenyi_core.pipeline.orchestrator import Orchestrator

        config = context().load_config()
        require_input_file(input, console=console)
        fmt = resolve_output_format(input, fmt, console=console)
        if mono is not None:
            config.output.mono = mono
        if bilingual is not None:
            config.output.bilingual = bilingual
        try:
            result = Orchestrator(config, client=FakeClient()).run_assemble(
                input,
                out_format=fmt,
                out_path=resolve_export_out_path(out, overwrite=overwrite, console=console),
            )
        except ExportPartialError as error:
            for outcome in error.outcomes:
                if outcome.ok:
                    console.print(
                        f"[green]{outcome.product} {outcome.out_format} written:[/]"
                        f" [bold]{outcome.output}[/]"
                    )
                else:
                    console.print(
                        f"[red]{outcome.product} {outcome.out_format} failed:[/] {outcome.error}"
                    )
            raise typer.Exit(1) from None
        except (IngestError, OSError, ValueError) as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None
        paths = result["outputs"]
        presentation.print_timing(console, load_timing(result["store"].run_dir))
        for path in paths:
            console.print(f"Translation written: [bold]{path}[/]")
