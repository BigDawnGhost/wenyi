"""Configuration and preflight policy scoped to one Click invocation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import typer
import yaml
from rich.console import Console
from typer.main import get_current_context
from wenyi_core.config import Config
from wenyi_core.i18n.genres import DEFAULT_GENRE


@dataclass
class CommandContext:
    config_path: str
    skip_api_check: bool
    console: Console
    experimental_prompt_genre: str = DEFAULT_GENRE

    def load_config(self) -> Config:
        """Load this invocation's config and apply its non-persisted CLI experiment."""
        try:
            config = Config.load(self.config_path)
            config.set_experimental_prompt_genre(self.experimental_prompt_genre)
            return config
        except (OSError, ValueError, yaml.YAMLError) as error:
            self.console.print(f"[red]Configuration error: {error}[/]")
            raise typer.Exit(1) from None

    def validate_api_configuration(self, config: Config, workflow: str) -> None:
        """Validate only the connections reachable after command-line overrides."""
        from wenyi_core.llm.factory import build_client
        from wenyi_core.llm.operations import configured_operations

        if not self.skip_api_check:
            try:
                build_client(config).validate_credentials(configured_operations(config, workflow))
            except (ValueError, RuntimeError) as error:
                self.console.print(f"[red]Error: {error}[/]")
                raise typer.Exit(1) from None


ContextAccessor = Callable[[], CommandContext]


def current_context() -> CommandContext:
    """Return the root invocation data, including during eager option callbacks."""
    context = get_current_context().find_root().obj
    if not isinstance(context, CommandContext):
        raise RuntimeError("The CLI invocation has no CommandContext")
    return context
