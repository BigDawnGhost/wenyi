"""Authentication helpers for providers that use interactive login instead of API keys."""

from __future__ import annotations

import json
from collections.abc import Callable

import typer
from rich.console import Console

from ..config import Config


def register_auth_commands(
    app: typer.Typer, load_config: Callable[[], Config], console: Console
) -> None:
    """Register `auth` commands (currently Codex ChatGPT login)."""
    del load_config  # Reserved for future provider-scoped config checks.
    auth = typer.Typer(help="Sign in to optional providers that use interactive authentication.")
    app.add_typer(auth, name="auth", rich_help_panel="Configuration")

    codex = typer.Typer(help="OpenAI Codex (ChatGPT subscription) authentication.")
    auth.add_typer(codex, name="codex")

    @codex.command("status")
    def status(as_json: bool = typer.Option(False, "--json")) -> None:
        """Show whether the official openai-codex SDK has a ChatGPT session."""
        from ..llm.providers import codex as codex_provider

        try:
            payload = codex_provider.account_status()
        except Exception as error:  # noqa: BLE001 - CLI surfaces install/auth errors plainly.
            console.print(f"[red]{error}[/red]")
            raise typer.Exit(1) from None
        if as_json:
            console.print_json(json.dumps(payload))
            return
        if payload.get("signed_in"):
            email = payload.get("email") or "(unknown email)"
            plan = payload.get("plan_type") or "(unknown plan)"
            console.print(f"[green]Signed in[/green] as {email} ({plan})")
        else:
            console.print("[yellow]Not signed in[/yellow]. Run: trans-novel auth codex login")

    @codex.command("login")
    def login(
        device_code: bool = typer.Option(
            False,
            "--device-code",
            help="Use device-code login for SSH/headless environments",
        ),
    ) -> None:
        """Sign in with ChatGPT through the official openai-codex SDK."""
        from ..llm.providers import codex as codex_provider

        try:
            result = codex_provider.login_chatgpt(device_code=device_code)
        except Exception as error:  # noqa: BLE001 - CLI surfaces SDK failures plainly.
            console.print(f"[red]{error}[/red]")
            raise typer.Exit(1) from None
        if result.get("success"):
            console.print("[green]Codex ChatGPT login succeeded[/green]")
            return
        console.print("[red]Codex ChatGPT login did not complete[/red]")
        raise typer.Exit(1)

    @codex.command("logout")
    def logout() -> None:
        """Clear the Codex SDK ChatGPT session."""
        from ..llm.providers import codex as codex_provider

        try:
            codex_provider.logout_chatgpt()
        except Exception as error:  # noqa: BLE001 - CLI surfaces SDK failures plainly.
            console.print(f"[red]{error}[/red]")
            raise typer.Exit(1) from None
        console.print("[green]Signed out of Codex ChatGPT[/green]")
