"""Construct the command-line application and expose the installed entry point."""

from __future__ import annotations

import typer
from rich.console import Console

from .commands.bootstrap import configure_windows_console, initializing_group, register_bootstrap
from .commands.context import current_context
from .commands.glossary import register_glossary_commands
from .commands.inspection import register_inspection_commands
from .commands.workflows import register_workflows_commands
from .model_commands import register_model_commands


def create_app(*, console: Console | None = None) -> typer.Typer:
    """Build an independent application with fresh context on every invocation."""
    configure_windows_console()
    console = console or Console()
    application = typer.Typer(
        cls=initializing_group(console),
        add_completion=False,
        no_args_is_help=True,
        help="Multilingual translation workflows for long-form fiction.",
    )
    register_bootstrap(application)
    register_model_commands(application, lambda: current_context().load_config(), console)
    register_workflows_commands(application, current_context)
    register_inspection_commands(application, current_context)
    register_glossary_commands(application, current_context)
    return application


app = create_app()


def main() -> None:
    """Start the Typer command-line application."""
    app()


if __name__ == "__main__":
    main()
