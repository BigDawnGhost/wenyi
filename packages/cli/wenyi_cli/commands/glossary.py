"""Register glossary commands with an invocation context accessor."""

from __future__ import annotations

import typer
from rich.table import Table

from .context import ContextAccessor
from .validation import (
    runstore_for_cli,
)


def register_glossary_commands(app: typer.Typer, context: ContextAccessor) -> None:
    glossary_app = typer.Typer(
        add_completion=False,
        no_args_is_help=True,
        help="Inspect glossary entries, check conflicts and resolve translations.",
    )
    app.add_typer(glossary_app, name="glossary", rich_help_panel="Glossary")

    @glossary_app.command("list")
    def glossary_list(
        input: str = typer.Argument(..., help="Source file with existing translation state"),
    ) -> None:
        """List established glossary translations and their status."""
        console = context().console
        from wenyi_core.glossary.store import GlossaryStore

        config = context().load_config()
        store = runstore_for_cli(config, input, console=console)
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
        console = context().console
        from wenyi_core.glossary.store import GlossaryStore

        config = context().load_config()
        store = runstore_for_cli(config, input, console=console)
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
        target: str = typer.Argument(
            ..., help="Target translation to use consistently from now on"
        ),
    ) -> None:
        """Resolve an existing term to a chosen translation and close its conflicts."""
        console = context().console
        from wenyi_core.glossary import resolver
        from wenyi_core.glossary.store import GlossaryStore

        config = context().load_config()
        store = runstore_for_cli(config, input, console=console)
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
