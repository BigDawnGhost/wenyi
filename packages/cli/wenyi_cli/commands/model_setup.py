"""Interactive provider, credential and model selection behind ``wenyi model``.

The command walks the path a manual setup would: choose a provider, supply its credential
(an API key, a subscription sign-in or an import), use the built-in endpoint (or ask for a
custom endpoint when none is declared), then pick one model
for every tier and override individual tiers only when they should differ. A credential is
written to the ``.env`` file beside the selected configuration, so later shells need no manual
``export``; only the ``llm`` block of that configuration is rewritten.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table
from wenyi_core.config import Config, write_llm_section
from wenyi_core.envfile import env_file_path, write_env_values
from wenyi_core.llm import selection, subscriptions
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.oauth.credentials import OAuthCredential, OAuthCredentialError
from wenyi_core.llm.oauth.flows import DeviceCodePrompt
from wenyi_core.llm.oauth.importers import read_credential_files
from wenyi_core.llm.operations import TIERS
from wenyi_core.llm.registry import provider_spec
from wenyi_core.llm.selection import ProviderChoice

from .oauth_ui import print_device_prompt
from .selection_prompt import select

SELECTION_KEYS = ("preset", "providers", "models", "tiers")


def register_model_setup_command(
    app: typer.Typer,
    load_config: Callable[[], Config],
    config_path: Callable[[], str],
    console: Console,
) -> None:
    """Register the interactive entry point for choosing providers and models."""

    @app.command("model", rich_help_panel="Configuration")
    def select_model(
        status: bool = typer.Option(False, "--status", help="Print the current selection and exit"),
        as_json: bool = typer.Option(
            False, "--json", help="Print structured data instead of tables (implies --status)"
        ),
        provider: str | None = typer.Option(
            None, "--provider", help="Provider kind or alias to select without prompting"
        ),
        model: str | None = typer.Option(
            None, "--model", help="Model every tier uses unless a tier overrides it"
        ),
        strong: str | None = typer.Option(
            None, "--strong", help="Model for the strong tier, overriding --model"
        ),
        cheap: str | None = typer.Option(
            None, "--cheap", help="Model for the cheap tier, overriding --model"
        ),
        fast: str | None = typer.Option(
            None, "--fast", help="Model for the fast tier, overriding --model"
        ),
        base_url: str | None = typer.Option(
            None, "--base-url", help="Endpoint stored for the selected connection"
        ),
        api_key: str | None = typer.Option(
            None, "--api-key", help="Store this credential for the selected provider"
        ),
        credential: bool = typer.Option(
            True,
            "--credential/--no-credential",
            help="Read or store a credential for the selected provider",
        ),
        offline: bool = typer.Option(
            False, "--offline", help="Offer the declared models instead of the provider catalog"
        ),
        every: bool = typer.Option(False, "--all", help="List every registered provider"),
        assume_yes: bool = typer.Option(
            False, "--yes", "-y", help="Write the configuration without confirming"
        ),
    ) -> None:
        """Choose a provider, supply its credential and pick the model every tier uses.

        Any tier can be pointed at a different model; per-operation routing is unchanged and
        stays visible through `wenyi models list`.
        """
        config = load_config()
        choices = selection.provider_choices()
        if status or as_json:
            _print_state(console, config, choices, as_json=as_json, every=every)
            return

        path = config_path()
        selected = _choose_provider(console, choices, config, provider=provider, every=every)
        _print_banner(console, config, choices, selected)
        if credential:
            _prepare_credential(console, selected, path, api_key=api_key)
        endpoint = _choose_endpoint(console, selected, config, base_url)
        tier_models = _choose_models(
            console,
            selected,
            config,
            {"model": model, "strong": strong, "cheap": cheap, "fast": fast},
            offline=offline,
            assume_yes=assume_yes,
            base_url=endpoint,
        )
        try:
            section = selection.llm_section(selected.kind, tier_models, base_url=endpoint)
        except ValueError as error:
            console.print(f"[red]Error: {error}[/]")
            raise typer.Exit(1) from None
        if not _write(console, path, section, assume_yes=assume_yes):
            raise typer.Exit(1)
        _print_state(
            console, Config.load(path), selection.provider_choices(), as_json=False, every=False
        )


def _print_state(
    console: Console,
    config: Config,
    choices: tuple[ProviderChoice, ...],
    *,
    as_json: bool,
    every: bool,
) -> None:
    """Report the active tiers, their credentials and the providers on offer."""
    tiers = _active_tiers(config)
    active_kinds = {connection.kind for connection in config.llm.providers.values()}
    highlighted = [choice for choice in choices if choice.configured or choice.kind in active_kinds]
    listed = list(choices) if every or not highlighted else highlighted
    hidden = len(choices) - len(listed)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "preset": config.llm.preset,
                    "tiers": tiers,
                    "providers": [choice.describe() for choice in listed],
                    "hidden_providers": hidden,
                },
                indent=2,
            )
        )
        return

    console.print(f"Preset: [bold]{config.llm.preset or 'none (explicit connections)'}[/]")
    table = Table("Tier", "Provider", "Model", "Credential")
    by_kind = {choice.kind: choice for choice in choices}
    for tier in TIERS:
        route = tiers[tier]
        choice = by_kind.get(route["kind"])
        table.add_row(
            tier,
            f"{choice.display_name} ({route['kind']})" if choice else route["kind"] or "—",
            route["model"] or "—",
            _credential_text(choice, route["kind"]),
        )
    console.print(table)

    providers = Table("Provider", "Credential")
    for choice in listed:
        providers.add_row(
            f"{choice.display_name} ({choice.kind})",
            _credential_text(choice, choice.kind),
        )
    console.print(providers)
    if hidden:
        console.print(f"{hidden} more registered providers; pass --all to list them.")
    console.print(
        "Change the selection with `wenyi model`; per-operation routing with `wenyi models list`."
    )


def _active_tiers(config: Config) -> dict[str, dict[str, str]]:
    """Describe the model and connection behind each configured tier."""
    tiers: dict[str, dict[str, str]] = {}
    for tier in TIERS:
        model_id = config.llm.tiers.get(tier)
        model = config.llm.models.get(model_id) if model_id else None
        connection = config.llm.providers.get(model.provider) if model is not None else None
        tiers[tier] = {
            "model_config": model_id or "",
            "connection": model.provider if model is not None else "",
            "kind": connection.kind if connection is not None else "",
            "model": model.model if model is not None else "",
        }
    return tiers


def _print_banner(
    console: Console,
    config: Config,
    choices: tuple[ProviderChoice, ...],
    selected: ProviderChoice,
) -> None:
    """Announce the route in use, then the provider this run is configuring."""
    tiers = _active_tiers(config)
    by_kind = {choice.kind: choice for choice in choices}
    active = tiers["strong"]
    named = by_kind.get(active["kind"])
    console.print(f"Current model:    [bold]{active['model'] or 'unset'}[/]")
    console.print(
        f"Active provider:  {named.display_name if named else active['kind'] or 'none'} "
        f"({active['kind'] or 'none'})"
    )
    description = f" — {selected.description}" if selected.description else ""
    console.print(
        f"Selected provider: [bold]{selected.display_name}[/] ({selected.kind}){description}"
    )


def _credential_text(choice: ProviderChoice | None, kind: str) -> str:
    """Describe where a provider credential comes from, or how to obtain one."""
    if choice is None:
        return "—"
    if choice.credential_env:
        return f"{choice.credential_env} (set)"
    if choice.sign_in:
        return f"sign in as `{choice.sign_in}`"
    if not choice.requires_api_key:
        return f"no credential needed ({kind})"
    if choice.api_key_envs:
        return f"{choice.api_key_envs[0]} (unset)"
    return f"no credential variable ({kind})"


def _credential_hint(choice: ProviderChoice) -> str:
    """Return the next step for a provider that still has no usable credential."""
    if choice.sign_in:
        return (
            f"[yellow]No credential stored for {choice.display_name}; rerun `wenyi model` to "
            f"sign in, or run `wenyi auth login {choice.sign_in}`.[/]"
        )
    if choice.api_key_envs:
        return (
            f"[yellow]No credential stored for {choice.display_name}; rerun `wenyi model` to "
            f"save one, or export {choice.api_key_envs[0]} in your shell.[/]"
        )
    return f"[yellow]{choice.display_name} declares no credential variable.[/]"


def _choose_provider(
    console: Console,
    choices: tuple[ProviderChoice, ...],
    config: Config,
    *,
    provider: str | None,
    every: bool,
) -> ProviderChoice:
    """Resolve an explicit provider or offer every registered provider with arrow keys."""
    if provider is not None:
        return _resolve_choice(choices, provider)
    active_kinds = {connection.kind for connection in config.llm.providers.values()}
    options = list(choices)
    default_index = next(
        (index for index, choice in enumerate(options) if choice.kind in active_kinds), 0
    )
    index = select(
        console,
        "Provider",
        [f"{choice.display_name} ({choice.kind})" for choice in options],
        default=default_index,
    )
    if index is None:
        raise typer.Exit(1)
    return options[index]


def _resolve_choice(choices: tuple[ProviderChoice, ...], name: str) -> ProviderChoice:
    """Look up one provider by kind, alias or login name."""
    try:
        kind = provider_spec(name).kind
    except ValueError as error:
        raise typer.BadParameter(str(error)) from None
    for choice in choices:
        if choice.kind == kind:
            return choice
    raise typer.BadParameter(f"Provider {name!r} is not routable")


def _prepare_credential(
    console: Console,
    choice: ProviderChoice,
    config_path: str,
    *,
    api_key: str | None,
) -> None:
    """Obtain the provider's credential and store it in the file beside the configuration."""
    if choice.credential_env and not api_key:
        console.print(f"{choice.display_name} credential: [bold]{choice.credential_env}[/] (set)")
        if not _confirm(console, "Replace it?", default=False):
            return
    obtained = (
        _credential_from_flag(choice, api_key) if api_key else _obtain_credential(console, choice)
    )
    if obtained is None:
        console.print(_credential_hint(choice))
        return
    variable, value = obtained
    path = env_file_path(config_path)
    try:
        write_env_values(path, {variable: value})
    except (OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None
    os.environ[variable] = value
    console.print(f"Stored [bold]{variable}[/] in {path}")
    console.print("Credential saved securely; continuing to model selection.")


def _credential_from_flag(choice: ProviderChoice, api_key: str) -> tuple[str, str] | None:
    """Return where a credential passed on the command line belongs."""
    target = _api_key_target(choice)
    if target is None:
        raise typer.BadParameter(f"{choice.display_name} accepts no stored credential")
    return target, api_key


def _obtain_credential(console: Console, choice: ProviderChoice) -> tuple[str, str] | None:
    """Ask where the credential comes from and return the ``(variable, value)`` to store."""
    subscription = _subscription(choice)
    actions: list[tuple[str, Callable[[], tuple[str, str] | None]]] = []
    if choice.sign_in and choice.oauth_env:
        actions.append((f"Sign in with {choice.display_name}", lambda: _sign_in(console, choice)))
    if subscription is not None and subscription.credential_files is not None:
        actions.append(("Import the credential the client wrote", lambda: _import(console, choice)))
    if _api_key_target(choice) is not None or choice.oauth_env is not None:
        actions.append(("Paste an API key or token", lambda: _paste(console, choice)))
    if not actions:
        return None

    console.print(f"No credential found for {choice.display_name}.")
    if choice.signup_url:
        console.print(f"  Get one at: {choice.signup_url}")
    index = select(console, "Credential source", [label for label, _ in actions])
    if index is None:
        return None
    return actions[index][1]()


def _subscription(choice: ProviderChoice) -> subscriptions.Subscription | None:
    """Return the subscription record that owns this provider's sign-in, when it has one."""
    return subscriptions.SUBSCRIPTIONS_BY_KIND.get(choice.kind)


def _api_key_target(choice: ProviderChoice) -> str | None:
    """Return the variable a pasted API key belongs in, when the provider accepts one."""
    return choice.api_key_envs[0] if choice.api_key_envs else None


def _sign_in(console: Console, choice: ProviderChoice) -> tuple[str, str] | None:
    """Run the provider's interactive grant and return the credential to store."""
    subscription = _subscription(choice)
    if subscription is None or choice.oauth_env is None:
        return None
    try:
        credential = subscriptions.login(
            subscription.kind,
            on_prompt=lambda prompt: _print_prompt(console, prompt),
            on_authorize=lambda url: console.print(f"Open this URL to authorize:\n[bold]{url}[/]"),
        )
    except OAuthCredentialError as error:
        console.print(f"[red]Error: {error}[/]")
        raise typer.Exit(1) from None
    console.print(f"Signed in to {choice.display_name}; authorization completed.")
    return choice.oauth_env, _credential_payload(credential)


def _import(console: Console, choice: ProviderChoice) -> tuple[str, str] | None:
    """Import the credential a first-party client already wrote, when one can be read."""
    subscription = _subscription(choice)
    if subscription is None or subscription.credential_files is None or choice.oauth_env is None:
        return None
    paths = subscription.credential_files()
    credentials, warnings = read_credential_files(paths)
    for warning in warnings:
        console.print(f"[yellow]Skipped {warning}[/]")
    if not credentials:
        console.print(f"[yellow]No credential found in {', '.join(str(path) for path in paths)}[/]")
        return None
    return choice.oauth_env, _credential_payload(credentials[0])


def _paste(console: Console, choice: ProviderChoice) -> tuple[str, str] | None:
    """Read a key or token from the terminal and return it with its variable name."""
    target = _api_key_target(choice) or choice.oauth_env
    if target is None:
        return None
    label = (
        f"{choice.display_name} credential"
        if target == choice.oauth_env
        else f"{choice.display_name} API key"
    )
    value = _ask_secret(console, label)
    if not value:
        return None
    return target, value


def _credential_payload(credential: OAuthCredential) -> str:
    """Serialize one credential the way the credential environment variable stores it."""
    return json.dumps(credential.to_payload(), ensure_ascii=False, separators=(",", ":"))


def _print_prompt(console: Console, prompt: DeviceCodePrompt) -> None:
    print_device_prompt(console, prompt)


def _choose_endpoint(
    console: Console,
    choice: ProviderChoice,
    config: Config,
    provided: str | None,
) -> str | None:
    """Resolve the connection endpoint; ``None`` keeps whatever the provider declares."""
    current = _configured_base_url(config, choice.kind)
    default = provided or current or selection.effective_base_url(choice.kind) or ""
    if not provided and default:
        return current
    if not provided and choice.sign_in:
        return None
    while True:
        if provided:
            candidate = provided
        else:
            answer = _ask(console, "Base URL", default=default)
            candidate = (answer if answer is not None else default).strip()
        if not candidate:
            if choice.base_url is None:
                console.print(
                    f"[yellow]{choice.display_name} needs an endpoint; pass --base-url.[/]"
                )
            return None
        try:
            ProviderConfig(kind=choice.kind, base_url=candidate)
        except ValidationError as error:
            message = _validation_message(error)
            if provided:
                console.print(f"[red]Error: {message}[/]")
                raise typer.Exit(1) from None
            console.print(f"[yellow]{message}[/]")
            continue
        return None if candidate == selection.effective_base_url(choice.kind) else candidate


def _validation_message(error: ValidationError) -> str:
    """Return the first validation complaint without pydantic's documentation footer."""
    first = error.errors()[0]
    return str(first.get("msg") or error).removeprefix("Value error, ")


def _configured_base_url(config: Config, kind: str) -> str | None:
    """Return the endpoint override the current configuration stores for this provider."""
    for connection in config.llm.providers.values():
        if connection.kind == kind and connection.base_url:
            return connection.base_url
    return None


def _choose_models(
    console: Console,
    choice: ProviderChoice,
    config: Config,
    provided: Mapping[str, str | None],
    *,
    offline: bool,
    assume_yes: bool,
    base_url: str | None = None,
) -> dict[str, str]:
    """Choose from a live catalog or manual IDs; defaults require explicit offline mode."""
    with console.status("Fetching models from the selected provider..."):
        candidates, failure = selection.candidate_models(
            choice.kind, offline=offline, base_url=base_url
        )
    if failure:
        console.print(f"[yellow]{failure}; enter a model ID manually or retry the command.[/]")
    if offline:
        console.print("Offline mode: showing declared models, not a live catalog.")
    elif candidates:
        console.print(f"Fetched {len(candidates)} models from the selected endpoint.")
    else:
        console.print("No live model list is available; enter a model ID manually.")
    if choice.notes:
        console.print(f"[dim]{choice.notes}[/]")
    defaults = _tier_defaults(config, choice) if offline else {tier: "" for tier in TIERS}
    models = {tier: (provided[tier] or defaults[tier]) for tier in TIERS}
    if provided["model"]:
        models = {tier: (provided[tier] or provided["model"]) for tier in TIERS}
    if not assume_yes:
        default_model = _prompt_default_model(
            console, candidates, provided["model"] or defaults["strong"], choice
        )
        if default_model:
            models = {tier: (provided[tier] or default_model) for tier in TIERS}
        _edit_tiers(console, choice, candidates, models, defaults)
    missing = [tier for tier in TIERS if not models[tier]]
    if missing:
        raise typer.BadParameter(
            f"No {missing[0]} model was provided; pass --model to use one model for every tier"
        )
    return models


def _prompt_default_model(
    console: Console,
    candidates: tuple[str, ...],
    current: str,
    choice: ProviderChoice,
) -> str:
    """Ask for the model every tier shares; ``""`` keeps each tier's declared default."""
    console.print("Pick one model for every tier, then optionally override individual tiers.")
    chosen = _prompt_model(console, "Default model", candidates, current, choice)
    return chosen or ""


def _tier_defaults(config: Config, choice: ProviderChoice) -> dict[str, str]:
    """Preset models, replaced by the current selection when it already uses this provider."""
    defaults = {tier: choice.preset_models.get(tier, "") for tier in TIERS}
    if not any(connection.kind == choice.kind for connection in config.llm.providers.values()):
        return defaults
    for tier in TIERS:
        model_id = config.llm.tiers.get(tier)
        model = config.llm.models.get(model_id) if model_id else None
        connection = config.llm.providers.get(model.provider) if model is not None else None
        if connection is not None and connection.kind == choice.kind:
            defaults[tier] = model.model
    return defaults


def _edit_tiers(
    console: Console,
    choice: ProviderChoice,
    candidates: tuple[str, ...],
    models: dict[str, str],
    defaults: Mapping[str, str],
) -> None:
    """Show what each tier resolves to and let the user override any of them."""
    while True:
        table = Table("Tier", "Model", "Source")
        for tier in TIERS:
            table.add_row(
                tier,
                models[tier] or "[red]unset[/]",
                "declared" if models[tier] == defaults[tier] else "chosen",
            )
        console.print(table)
        index = select(console, "Override a tier", ["Finish", *TIERS])
        if index is None or index == 0:
            break
        tier = TIERS[index - 1]
        chosen = _prompt_model(console, tier, candidates, models[tier], choice)
        if chosen is not None:
            models[tier] = chosen


def _prompt_model(
    console: Console,
    label: str,
    candidates: tuple[str, ...],
    current: str,
    choice: ProviderChoice,
) -> str | None:
    """Ask for one model by label; ``None`` keeps the current value."""
    options = [*candidates, "Enter a custom model name"]
    if current:
        options.append(f"Keep {current}")
    default = candidates.index(current) if current in candidates else 0
    index = select(console, label, options, default=default)
    if index is None:
        return None
    if index < len(candidates):
        return candidates[index]
    if index == len(candidates):
        custom = _ask(console, "Model name", default=current)
        return (custom or "").strip() or None
    return None


def _write(console: Console, path: str, section: Mapping[str, object], *, assume_yes: bool) -> bool:
    """Preview the new ``llm`` block, then replace it after confirmation."""
    target = Path(path)
    previous = target.read_text(encoding="utf-8") if target.is_file() else ""
    console.print(f"New [bold]llm[/] block for {path}:")
    console.print(Syntax(yaml.safe_dump({"llm": dict(section)}, sort_keys=False), "yaml"))
    dropped = _dropped_keys(previous)
    if dropped:
        console.print(f"[yellow]This replaces existing llm keys: {', '.join(dropped)}[/]")
    if not assume_yes and not _confirm(console, f"Write {path}?", default=False):
        console.print("Nothing written.")
        return False
    try:
        write_llm_section(path, section)
    except (OSError, ValueError) as error:
        console.print(f"[red]Error: {error}[/]")
        return False
    console.print(f"Updated [bold]{path}[/]")
    return True


def _dropped_keys(text: str) -> list[str]:
    """List existing ``llm`` keys outside the selection the picker writes."""
    try:
        raw = yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError:
        return []
    llm = raw.get("llm") if isinstance(raw, dict) else None
    if not isinstance(llm, dict):
        return []
    return sorted(set(llm) - set(SELECTION_KEYS))


def _ask(console: Console, label: str, *, default: str = "") -> str | None:
    """Read one answer; ``None`` reports end of input so callers apply their own policy."""
    try:
        return Prompt.ask(
            label, console=console, default=default, show_default=bool(default)
        ).strip()
    except EOFError:
        return None


def _ask_secret(console: Console, label: str) -> str | None:
    """Read one credential; masking needs a terminal, so a pipe reads it in the clear."""
    masked = console.is_terminal and sys.stdin.isatty()
    try:
        return Prompt.ask(label, console=console, password=masked).strip()
    except EOFError:
        return None


def _confirm(console: Console, label: str, *, default: bool) -> bool:
    try:
        return bool(Confirm.ask(label, console=console, default=default))
    except EOFError:
        return default


__all__ = ["register_model_setup_command"]
