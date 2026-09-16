"""Early configuration creation, platform console setup and root CLI options."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from importlib.metadata import version as package_version
from typing import Any

import typer
from rich.console import Console
from typer.core import TyperGroup
from wenyi_core.config import Config

from .context import CommandContext, current_context


def configure_windows_console(
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


def config_path_from_args(args: Sequence[str]) -> str:
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


def initializing_group(console: Console) -> type[TyperGroup]:
    """Bind application presentation while allocating invocation data anew in main."""

    class ConfigInitializingGroup(TyperGroup):
        """Check the default configuration before Click dispatches or exits early."""

        def main(
            self,
            args: Sequence[str] | None = None,
            *main_args: Any,
            **main_kwargs: Any,
        ) -> Any:
            """Locate and create a missing default config before Click parses the command."""
            cli_args = list(args) if args is not None else sys.argv[1:]
            config_path = config_path_from_args(cli_args)
            main_kwargs["obj"] = CommandContext(
                config_path, any(arg in {"--help", "-h"} for arg in cli_args), console
            )
            Config.create_default_file(config_path)
            from wenyi_core.llm.limits import RequestStopped

            try:
                return super().main(*main_args, args=args, **main_kwargs)
            except RequestStopped as error:
                typer.echo(f"Stopped: {error}", err=True)
                raise SystemExit(1) from None

    return ConfigInitializingGroup


def register_bootstrap(app: typer.Typer) -> None:
    def _version_callback(value: bool) -> None:
        """Print the installed package version derived from Git tags and exit."""
        if value:
            current_context().console.print(package_version("wenyi-cli"))
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
        current_context().config_path = config
