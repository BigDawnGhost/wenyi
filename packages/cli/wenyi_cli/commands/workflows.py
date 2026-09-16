"""Register workflows commands with an invocation context accessor."""

from __future__ import annotations

import os

import typer
from rich.progress import Progress
from wenyi_core.ingest.errors import IngestError
from wenyi_core.timing import load_timing

from wenyi_cli.commands import presentation
from wenyi_cli.commands import progress as progress_view

from .context import ContextAccessor
from .validation import (
    require_input_file,
    resolve_output_format,
    validate_pdf_engine,
)


def register_workflows_commands(app: typer.Typer, context: ContextAccessor) -> None:
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
        console = context().console
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
        console = context().console
        from wenyi_core.srt.translate import translate_srt

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

        config = context().load_config()
        context().validate_api_configuration(config, "srt")
        require_input_file(input_path, console=console)
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
        console = context().console
        from wenyi_core.pipeline.orchestrator import Orchestrator

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

        fmt = resolve_output_format(input_path, fmt, console=console)
        pdf_engine = validate_pdf_engine(pdf_engine, console=console)
        config = context().load_config()
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
        context().validate_api_configuration(config, "translate")
        require_input_file(input_path, console=console)
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
        console = context().console
        from wenyi_core.pipeline.orchestrator import Orchestrator

        try:
            config = context().load_config()
            context().validate_api_configuration(config, "prepare")
            require_input_file(input_path, console=console)
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
        console = context().console
        from wenyi_core.pipeline.orchestrator import Orchestrator

        try:
            config = context().load_config()
            if autofix is not None:
                config.pipeline.review_autofix = autofix
            context().validate_api_configuration(config, "review")
            require_input_file(input, console=console)
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
