"""Subscription sign-in, credential import and offline credential checks.

Wenyi never stores a credential itself: an OAuth credential lives in the environment
variable a provider profile names, and every command here ends in the shell assignment the
user exports. Sign-in talks to the provider, import and check only read local files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from wenyi_core.llm.oauth.credentials import OAuthCredential, OAuthCredentialError
from wenyi_core.llm.oauth.importers import parse_credential_text, read_credential_files
from wenyi_core.llm.subscriptions import (
    SUBSCRIPTIONS,
    Subscription,
    export_line,
    find_subscription,
    login,
)

from .oauth_ui import print_device_prompt


def register_auth_commands(app: typer.Typer, console: Console) -> None:
    """Register subscription sign-in, credential import and offline checks."""
    auth = typer.Typer(
        no_args_is_help=True,
        help="Sign in to subscription providers and import existing credentials.",
    )
    app.add_typer(auth, name="auth", rich_help_panel="Configuration")

    @auth.command("list")
    def list_subscriptions(
        as_json: bool = typer.Option(False, "--json", help="Print structured sign-in data"),
    ) -> None:
        """Show the available sign-ins and whether their credential variable is set."""
        rows = [
            _catalog(name, subscription) for name, subscription in sorted(SUBSCRIPTIONS.items())
        ]
        if as_json:
            typer.echo(json.dumps(rows, indent=2))
            return
        table = Table("Login", "Provider", "Credential variable", "Notes")
        for row in rows:
            table.add_row(
                row["login"],
                f"{row['display_name']} ({row['provider']})",
                f"{row['env_var']} ({'set' if row['configured'] else 'unset'})",
                row["notes"],
            )
        console.print(table)

    @auth.command("login")
    def authenticate(
        provider: str = typer.Argument(..., help="Sign-in name, provider kind or alias"),
        port: int | None = typer.Option(
            None, "--port", help="Loopback port for browser sign-in; ignored for device codes"
        ),
        timeout: float | None = typer.Option(
            None, "--timeout", help="Seconds to wait for approval"
        ),
        browser: bool = typer.Option(
            True, "--browser/--no-browser", help="Open the authorization URL in a browser"
        ),
        as_json: bool = typer.Option(False, "--json", help="Print structured credential data"),
    ) -> None:
        """Run a device or browser sign-in and print the credential export line."""
        subscription = _subscription(provider)
        try:
            credential = login(
                subscription.kind,
                on_prompt=lambda prompt: print_device_prompt(console, prompt, copy=browser),
                on_authorize=lambda url: console.print(
                    f"Open this URL to authorize:\n[bold]{url}[/]"
                ),
                open_browser=browser,
                port=port,
                timeout=timeout,
            )
        except OAuthCredentialError as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None
        _print_credential(console, subscription, credential, action="Signed in as", as_json=as_json)

    @auth.command("import")
    def import_credential(
        provider: str = typer.Argument(..., help="Sign-in name, provider kind or alias"),
        files: list[Path] | None = typer.Argument(
            None, help="Credential files; defaults to the client's own file"
        ),
        token: str | None = typer.Option(
            None, "--token", help="Import this token or credential text instead of a file"
        ),
        as_json: bool = typer.Option(False, "--json", help="Print structured credential data"),
    ) -> None:
        """Import an existing credential file or token and print the export line."""
        subscription = _subscription(provider)
        try:
            credential, warnings = _imported(subscription, files, token)
        except OAuthCredentialError as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None
        for warning in warnings:
            console.print(f"[yellow]Skipped {warning}[/]")
        _print_credential(console, subscription, credential, action="Imported", as_json=as_json)

    @auth.command("check")
    def check(
        provider: str | None = typer.Argument(None, help="Check one sign-in; default: all"),
        as_json: bool = typer.Option(False, "--json", help="Print structured report data"),
    ) -> None:
        """Report locally whether each credential variable holds a usable credential."""
        chosen = (
            [_subscription(provider)] if provider else sorted(SUBSCRIPTIONS.values(), key=_name)
        )
        reports = [_inspect(subscription) for subscription in chosen]
        if as_json:
            typer.echo(json.dumps(reports, indent=2))
        else:
            _print_reports(console, reports)
        if provider and reports[0]["status"] != "ready":
            raise typer.Exit(1)


def _name(subscription: Subscription) -> str:
    return subscription.display_name


def _catalog(login: str, subscription: Subscription) -> dict:
    """Describe one available sign-in without inspecting or touching any credential."""
    return {
        "login": login,
        "provider": subscription.kind,
        "display_name": subscription.display_name,
        "env_var": subscription.env_var,
        "configured": bool(os.environ.get(subscription.env_var, "").strip()),
        "notes": subscription.notes,
    }


def _subscription(provider: str) -> Subscription:
    subscription = find_subscription(provider)
    if subscription is None:
        available = ", ".join(sorted(SUBSCRIPTIONS))
        raise typer.BadParameter(f"Unknown sign-in {provider!r}; available: {available}")
    return subscription


def _print_credential(
    console: Console,
    subscription: Subscription,
    credential: OAuthCredential,
    *,
    action: str,
    as_json: bool,
) -> None:
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "env_var": subscription.env_var,
                    "credential": credential.to_payload(),
                },
                indent=2,
            )
        )
        return
    console.print(f"{action} [bold]{credential.describe()}[/]")
    console.print(f"Export this credential to use {subscription.display_name}:")
    console.print(export_line(subscription.env_var, credential))


def _imported(
    subscription: Subscription,
    files: list[Path] | None,
    token: str | None,
) -> tuple[OAuthCredential, list[str]]:
    """Return the first imported credential plus one warning per unusable source."""
    sources = list(files or ())
    if token:
        parsed = parse_credential_text(token)
        if parsed:
            return parsed[0], []
        raise OAuthCredentialError("The supplied --token is not a usable credential")
    if not sources:
        sources = list(subscription.credential_files() if subscription.credential_files else ())
        if not sources:
            raise OAuthCredentialError(
                f"{subscription.display_name} has no known credential file; pass one explicitly"
            )
    credentials, warnings = read_credential_files(sources)
    if not credentials:
        raise OAuthCredentialError(
            "No credential found in "
            + ", ".join(str(path) for path in sources)
            + "; run the first-party client once or pass --token"
        )
    return credentials[0], warnings


def _inspect(subscription: Subscription) -> dict:
    """Describe the local state of one credential variable without any network call."""
    report: dict = {
        "login": _login_name(subscription),
        "provider": subscription.kind,
        "display_name": subscription.display_name,
        "env_var": subscription.env_var,
        "configured": False,
        "status": "missing",
        "identity": None,
        "expired": None,
        "refreshable": None,
        "files": [str(path) for path in _existing_files(subscription)],
    }
    value = os.environ.get(subscription.env_var, "").strip()
    if not value:
        report["detail"] = f"{subscription.env_var} is not set"
        if report["files"]:
            report["detail"] = (
                f"{subscription.env_var} is not set; import the existing credential with "
                f"`wenyi auth import {report['login']}`"
            )
        return report
    report["configured"] = True
    credentials = parse_credential_text(value)
    if not credentials:
        report["status"] = "invalid"
        report["detail"] = f"{subscription.env_var} holds no usable access token"
        return report
    credential = credentials[0]
    report["identity"] = credential.describe()
    report["expired"] = credential.expired()
    report["refreshable"] = credential.refreshable()
    if not credential.access_token:
        report["status"] = "invalid"
        report["detail"] = "The credential has no access token; sign in again"
    elif not credential.expired() or credential.refreshable():
        report["status"] = "ready"
        report["detail"] = "Usable"
    else:
        report["status"] = "expired"
        report["detail"] = "Expired with no refresh token; sign in again"
    return report


def _login_name(subscription: Subscription) -> str:
    """Return the short login name users type for one subscription."""
    for name, candidate in SUBSCRIPTIONS.items():
        if candidate is subscription:
            return name
    return subscription.kind


def _existing_files(subscription: Subscription) -> tuple[Path, ...]:
    if subscription.credential_files is None:
        return ()
    return tuple(path for path in subscription.credential_files() if path.is_file())


_STATUS_STYLE: dict[str, str] = {
    "ready": "green",
    "missing": "yellow",
    "expired": "yellow",
    "invalid": "red",
}


def _print_reports(console: Console, reports: list[dict]) -> None:
    table = Table("Login", "Provider", "Status", "Credential variable", "Account", "Detail")
    for report in reports:
        style = _STATUS_STYLE.get(report["status"], "white")
        table.add_row(
            report["login"],
            report["display_name"],
            f"[{style}]{report['status']}[/]",
            report["env_var"],
            report["identity"] or "—",
            report["detail"],
        )
    console.print(table)
    console.print("Sign in with `wenyi auth login <login>`; credentials stay in your environment.")


__all__ = ["register_auth_commands"]
