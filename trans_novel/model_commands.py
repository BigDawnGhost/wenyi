"""Model configuration previews and explicit migration/comparison commands."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .config import Config
from .llm.operations import configured_operations, require_operation
from .llm.router import RoutedLLMClient
from .llm.routing import resolve_routes


def register_model_commands(
    app: typer.Typer, load_config: Callable[[], Config], console: Console
) -> None:
    """Register commands once; operation and provider details come from their registries."""
    models = typer.Typer(
        no_args_is_help=True, help="Inspect model routes and manage model settings."
    )
    app.add_typer(models, name="models", rich_help_panel="Configuration")

    @models.command("list")
    def list_models(
        as_json: bool = typer.Option(False, "--json", help="Print structured routing data"),
    ):
        """Show all effective routes without credentials or network requests."""
        config = load_config()
        routes = resolve_routes(config.llm)
        if as_json:
            typer.echo(
                json.dumps({key: value.describe() for key, value in routes.items()}, indent=2)
            )
            return
        table = Table(
            "Operation",
            "Selection",
            "Tier",
            "Profile",
            "Connection / adapter",
            "Model",
            "Output",
            "Concurrency",
        )
        for operation, route in routes.items():
            connection = config.llm.providers[route.provider]
            table.add_row(
                operation,
                route.origin,
                route.tier or "direct",
                route.profile,
                f"{route.provider} / {route.provider_kind}",
                route.model,
                str(route.max_output_tokens or "provider default"),
                str(connection.max_concurrency or "unlimited"),
            )
        console.print(table)

    @models.command("explain")
    def explain(operation: str = typer.Option(..., "--operation")):
        """Explain an operation's defaults, effective request and selection origin."""
        try:
            spec = require_operation(operation)
            route = resolve_routes(load_config().llm)[operation]
            typer.echo(json.dumps({"description": spec.description, **route.describe()}, indent=2))
        except ValueError as error:
            raise typer.BadParameter(str(error)) from None

    @models.command("check")
    def check(
        workflow: str = typer.Option(
            "translate", "--for", help="prepare, translate, review or srt"
        ),
    ):
        """Validate reachable credentials locally; never send a test request."""
        try:
            config = load_config()
            operations = configured_operations(config, workflow)
            RoutedLLMClient(config.llm).validate_credentials(operations)
            console.print(
                f"Model configuration valid for {workflow}: {len(operations)} operations."
            )
        except (ValueError, RuntimeError) as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None

    @models.command("migrate-config")
    def migrate_config(
        source: Path = typer.Argument(..., exists=True, dir_okay=False),
        out: Path = typer.Option(..., "--out", help="New YAML file; must not exist"),
    ):
        """Convert a retired configuration into a separate, reviewable YAML file."""
        from .llm.migration import convert_config

        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Configuration must be a mapping")
            converted = convert_config(raw)
            Config.from_dict(converted)
            with out.open("x", encoding="utf-8") as file:
                yaml.safe_dump(converted, file, allow_unicode=True, sort_keys=False)
            console.print(f"Converted configuration: {out}")
        except (OSError, ValueError, yaml.YAMLError) as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None

    @models.command("migrate-usage")
    def migrate_usage(run_dir: Path = typer.Argument(..., exists=True, file_okay=False)):
        """Back up and convert usage ledgers in one selected target run directory."""
        from .llm.usage import convert_usage_ledger
        from .pipeline.runstore import RunStore

        try:
            store = RunStore(str(run_dir), create=False)
            if not store.exists():
                raise ValueError("Select a target run directory containing manifest.json")
            with store.lock():
                paths = [
                    run_dir / "usage.json",
                    *sorted((run_dir / "reviews").glob("review-*/usage.json")),
                ]
                changes = []
                for path in paths:
                    if path.is_file():
                        original = path.read_bytes()
                        converted = convert_usage_ledger(json.loads(original))
                        if json.loads(original) != converted:
                            changes.append((path, original, converted))
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                for path, original, converted in changes:
                    with path.with_name(f"usage.before-routing-{stamp}.json").open("xb") as backup:
                        backup.write(original)
                    store._write_json(str(path), converted)
            console.print(
                f"Converted {len(changes)} usage ledgers; original files backed up alongside them."
            )
        except (OSError, ValueError) as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None

    @models.command("compare")
    def compare(
        operation: str = typer.Option(..., "--operation"),
        profiles: list[str] = typer.Option(..., "--model", help="Repeat for each profile"),
        messages: Path = typer.Option(
            ...,
            "--messages",
            exists=True,
            dir_okay=False,
            help="JSON message array to send to the selected models",
        ),
        out: Path = typer.Option(..., "--out", help="Comparison JSON file; must not exist"),
        json_mode: bool = typer.Option(False, "--json-mode"),
    ):
        """Send an explicit prompt fixture to models and record outputs, latency and usage."""
        try:
            require_operation(operation)
            if out.exists():
                raise ValueError("Comparison output already exists")
            payload = json.loads(messages.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, list)
                or not payload
                or any(
                    not isinstance(row, dict)
                    or set(row) != {"role", "content"}
                    or not isinstance(row["role"], str)
                    or row["role"] not in {"system", "user", "assistant"}
                    or not isinstance(row["content"], str)
                    for row in payload
                )
            ):
                raise ValueError("Messages must be a nonempty array of role/content objects")
            config = load_config()
            client = RoutedLLMClient(config.llm)
            routes = {profile: client.validate_profile(profile, operation) for profile in profiles}
            results = []
            with out.open("x", encoding="utf-8") as file, client.interrupt_scope():
                try:
                    for profile in profiles:
                        started = monotonic()
                        before = client.usage_summary()
                        from .llm.usage import usage_delta

                        row = {"profile": profile, "route": routes[profile].describe()}
                        try:
                            row["output"] = client.complete_profile(
                                payload, operation=operation, profile=profile, json_mode=json_mode
                            )
                        except Exception as error:
                            row["error"] = type(error).__name__
                        row["seconds"] = monotonic() - started
                        row["usage"] = usage_delta(client.usage_summary(), before)
                        results.append(row)
                finally:
                    json.dump(
                        {
                            "operation": operation,
                            "results": results,
                            "usage": client.usage_summary(),
                        },
                        file,
                        ensure_ascii=False,
                        indent=2,
                    )
            console.print(f"Model comparison saved: {out}")
        except (OSError, ValueError, RuntimeError) as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None
