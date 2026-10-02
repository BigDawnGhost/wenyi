"""Interactive provider, credential and model selection through ``wenyi model``."""

from __future__ import annotations

import json
import os
import re
import stat

import pytest
import yaml
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config
from wenyi_core.envfile import read_env_values

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\r")


def plain_text(value: str) -> str:
    """Strip ANSI/control sequences so CLI assertions stay stable under Rich."""
    return _ANSI_RE.sub("", value)


@pytest.fixture(autouse=True)
def declared_models_only(monkeypatch):
    """Keep the picker offline: the declared models stand in for the provider catalog."""
    from wenyi_core.llm import selection

    monkeypatch.setattr(selection, "list_models", lambda kind, timeout=20.0, base_url=None: ())


@pytest.fixture(autouse=True)
def restore_environment():
    """Undo the credential variables the bootstrap reads out of a temporary ``.env`` file."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def _invoke(tmp_path, raw, *arguments, input=None, online=False):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    flags = [] if online else ["--offline"]
    result = CliRunner().invoke(
        app, ["--config", str(config), "model", *flags, *arguments], input=input
    )
    return result, config


def test_status_shows_the_active_tiers_without_any_request(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr("openai.OpenAI", lambda **kw: pytest.fail("status constructed SDK"))

    result, _ = _invoke(tmp_path, {"llm": {"preset": "deepseek"}}, "--status")

    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "Preset: deepseek" in output
    assert "DEEPSEEK_API_KEY (set)" in output
    assert output.count("deepseek-flash") >= 3


def test_status_json_reports_tiers_providers_and_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result, _ = _invoke(tmp_path, {"llm": {"preset": "deepseek"}}, "--status", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["preset"] == "deepseek"
    assert payload["tiers"]["strong"]["model"] == "deepseek-flash"
    assert payload["tiers"]["strong"]["kind"] == "deepseek"
    providers = {entry["kind"]: entry for entry in payload["providers"]}
    assert providers["deepseek"]["configured"] is False
    assert providers["deepseek"]["credential_envs"] == ["DEEPSEEK_API_KEY"]
    assert providers["deepseek"]["api_key_envs"] == ["DEEPSEEK_API_KEY"]
    assert providers["deepseek"]["base_url"] == "https://api.deepseek.com/v1"
    assert payload["hidden_providers"] > 0


def test_selection_rewrites_the_llm_block_and_keeps_other_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    config = tmp_path / "config.yaml"
    config.write_text(
        "# Wenyi configuration\n"
        "language:\n"
        "  target: zh\n"
        "\n"
        "# ── LLM ──\n"
        "llm:\n"
        "  preset: deepseek\n"
        "\n"
        "# ── Pipeline ──\n"
        "pipeline:\n"
        "  review: true\n"
    )

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "model",
            "--offline",
            "--provider",
            "claude",
            "--strong",
            "claude-opus-4-6",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    text = config.read_text()
    assert "# Wenyi configuration" in text
    assert "# ── LLM ──" in text
    assert "# ── Pipeline ──" in text
    assert "review: true" in text
    assert "\n\n\n" not in text
    loaded = Config.load(str(config))
    assert loaded.llm.preset == "anthropic"
    assert loaded.llm.models["default_strong"].model == "claude-opus-4-6"
    assert loaded.llm.models["default_cheap"].model == "claude-haiku-4-5-20251001"


def test_selection_warns_about_replaced_llm_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    before = {
        "llm": {
            "preset": "deepseek",
            "budget": {"max_requests": 10},
            "routes": {"polish.body": {"tier": "cheap"}},
        }
    }

    result, config = _invoke(tmp_path, before, "--provider", "anthropic", "--yes")

    assert result.exit_code == 0, result.output
    assert "budget, routes" in plain_text(result.output)
    loaded = Config.load(str(config))
    assert loaded.llm.budget.max_requests is None
    assert loaded.llm.routes == {}


def test_selection_expands_providers_without_a_preset(tmp_path, monkeypatch):
    monkeypatch.setenv("WENYI_CODEX_OAUTH", '{"access_token": "token"}')

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "openai-codex",
        "--strong",
        "gpt-5-codex",
        "--cheap",
        "gpt-5-mini",
        "--fast",
        "gpt-5-mini",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    assert "No credential stored" not in plain_text(result.output)
    loaded = Config.load(str(config))
    assert loaded.llm.preset is None
    assert loaded.llm.providers["default"].kind == "openai-codex"
    assert loaded.llm.models[loaded.llm.tiers["strong"]].model == "gpt-5-codex"


def test_selection_requires_every_tier_for_a_presetless_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("WENYI_CODEX_OAUTH", '{"access_token": "token"}')
    before = {"llm": {"preset": "deepseek"}}

    result, config = _invoke(
        tmp_path, before, "--provider", "openai-codex", "--strong", "gpt-5-codex", "--yes"
    )

    assert result.exit_code == 2
    assert "No cheap model was provided" in plain_text(result.output)
    assert yaml.safe_load(config.read_text()) == before


def test_writing_needs_confirmation_without_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    before = {"llm": {"preset": "antigravity"}}

    result, config = _invoke(tmp_path, before, "--provider", "deepseek", input="n\n")

    assert result.exit_code == 1
    assert "Nothing written" in plain_text(result.output)
    assert yaml.safe_load(config.read_text()) == before


def test_missing_credentials_are_reported_before_writing(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result, config = _invoke(
        tmp_path, {"llm": {"preset": "deepseek"}}, "--provider", "anthropic", "--yes"
    )

    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    # Anthropic has no subscription sign-in here, so the hint must name its own variable.
    assert "No credential stored for Anthropic" in output
    assert "ANTHROPIC_API_KEY in your shell" in output
    assert not (tmp_path / ".env").exists()
    assert Config.load(str(config)).llm.preset == "anthropic"


def test_interactive_redirected_input_selects_a_provider_by_label(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

    result, config = _invoke(
        tmp_path, {"llm": {"preset": "anthropic"}}, "--yes", input="DeepSeek (deepseek)\n\n\n\n"
    )

    assert result.exit_code == 0, result.output
    # The active provider is offered first among the configured ones in registry order.
    assert Config.load(str(config)).llm.preset == "deepseek"


def test_all_lists_every_registered_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result, _ = _invoke(tmp_path, {"llm": {"preset": "deepseek"}}, "--status", "--all")

    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "OpenRouter (openrouter)" in output
    assert "more registered providers" not in output


def test_a_pasted_api_key_is_stored_beside_the_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        "--yes",
        input="\nsk-pasted\n\n",
    )

    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "No credential found for DeepSeek" in output
    assert "sk-pasted" not in output
    assert "Credential saved securely" in output
    assert read_env_values(tmp_path / ".env") == {"DEEPSEEK_API_KEY": "sk-pasted"}
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert Config.load(str(config)).llm.preset == "deepseek"


def test_the_api_key_flag_stores_a_credential_without_prompting(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result, _ = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "anthropic",
        "--api-key",
        "sk-ant-stored",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    assert read_env_values(tmp_path / ".env") == {"ANTHROPIC_API_KEY": "sk-ant-stored"}
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-stored"


def test_skipping_the_credential_step_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "anthropic",
        "--no-credential",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    assert "No credential found for Anthropic" not in plain_text(result.output)
    assert not (tmp_path / ".env").exists()
    assert Config.load(str(config)).llm.preset == "anthropic"


def test_an_existing_credential_is_kept_unless_replacement_is_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-shell")

    result, _ = _invoke(
        tmp_path, {"llm": {"preset": "deepseek"}}, "--provider", "deepseek", "--yes", input="\n"
    )

    assert result.exit_code == 0, result.output
    assert "DEEPSEEK_API_KEY (set)" in plain_text(result.output)
    assert not (tmp_path / ".env").exists()


def test_an_endpoint_override_reaches_the_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        "--base-url",
        "https://proxy.internal/v1",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    loaded = Config.load(str(config))
    assert loaded.llm.providers["default"].base_url == "https://proxy.internal/v1"
    assert loaded.llm.providers["default"].kind == "deepseek"


def test_an_endpoint_prompt_keeps_the_declared_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    assert "Base URL (" not in plain_text(result.output)
    # Pressing Enter must not freeze the provider default into the file.
    assert "providers" not in yaml.safe_load(config.read_text())["llm"]


def test_an_invalid_endpoint_is_rejected_without_a_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    before = {"llm": {"preset": "deepseek"}}

    result, config = _invoke(
        tmp_path, before, "--provider", "deepseek", "--base-url", "not-a-url", "--yes"
    )

    assert result.exit_code == 1
    output = plain_text(result.output)
    assert "absolute HTTP(S) endpoint" in output
    assert "Traceback" not in output
    assert yaml.safe_load(config.read_text()) == before


def test_every_tier_can_be_changed_in_one_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        # keep the credential, keep the endpoint, keep the default model, override fast and
        # cheap, finish, write.
        input="\n\nfast\ndeepseek-v4-pro\ncheap\ndeepseek-v4-pro\n\ny\n",
    )

    assert result.exit_code == 0, result.output
    loaded = Config.load(str(config))
    tiers = {
        tier: loaded.llm.models[reference].model for tier, reference in loaded.llm.tiers.items()
    }
    assert tiers["strong"] == "deepseek-flash"
    assert tiers["fast"] == "deepseek-v4-pro"
    assert tiers["cheap"] == "deepseek-v4-pro"


def test_the_default_model_fills_every_tier_without_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        # keep the credential and endpoint, choose the second declared model as the default,
        # then accept it for every tier.
        input="\ndeepseek-v4-pro\n\ny\n",
    )

    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "Pick one model for every tier" in output
    loaded = Config.load(str(config))
    tiers = {
        tier: loaded.llm.models[reference].model for tier, reference in loaded.llm.tiers.items()
    }
    assert set(tiers.values()) == {"deepseek-v4-pro"}


def test_the_model_flag_fills_every_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        "--model",
        "deepseek-v4-pro",
        "--yes",
    )

    assert result.exit_code == 0, result.output
    loaded = Config.load(str(config))
    tiers = {
        tier: loaded.llm.models[reference].model for tier, reference in loaded.llm.tiers.items()
    }
    assert set(tiers.values()) == {"deepseek-v4-pro"}


def test_accepting_the_default_keeps_each_tier_declared_model(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "anthropic",
        # keep the credential and endpoint, accept the shared default, finish, write.
        input="\n\n\ny\n",
    )

    assert result.exit_code == 0, result.output
    loaded = Config.load(str(config))
    tiers = {
        tier: loaded.llm.models[reference].model for tier, reference in loaded.llm.tiers.items()
    }
    assert tiers["strong"] == "claude-sonnet-4-6"
    assert tiers["cheap"] == "claude-sonnet-4-6"


def test_a_custom_model_name_can_be_typed(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        input="\n\nfast\nEnter a custom model name\nmy-private-model\n\ny\n",
    )

    assert result.exit_code == 0, result.output
    loaded = Config.load(str(config))
    assert loaded.llm.models[loaded.llm.tiers["fast"]].model == "my-private-model"


def test_a_provider_without_a_catalog_still_accepts_typed_models(tmp_path, monkeypatch):
    monkeypatch.delenv("WENYI_CODEX_OAUTH", raising=False)

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "openai-codex",
        "--no-credential",
        # keep the endpoint, type the default model, then override cheap and fast, write.
        input="\ngpt-5-codex\ncheap\n\ngpt-5-mini\nfast\n\ngpt-5-mini\n\ny\n",
        online=True,
    )

    assert result.exit_code == 0, result.output
    assert "No live model list" in plain_text(result.output)
    loaded = Config.load(str(config))
    assert loaded.llm.models[loaded.llm.tiers["strong"]].model == "gpt-5-codex"
    assert loaded.llm.models[loaded.llm.tiers["cheap"]].model == "gpt-5-mini"


def test_a_failed_live_list_accepts_a_manual_model_without_fallback(tmp_path, monkeypatch):
    from wenyi_core.llm import selection

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr(
        selection,
        "list_models",
        lambda kind, timeout=20.0, base_url=None: (_ for _ in ()).throw(ValueError("HTTP 500")),
    )

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "openrouter",
        "--model",
        "vendor/manual",
        "--yes",
        online=True,
    )

    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "HTTP 500" in output
    assert "enter a model ID manually" in output
    assert "offering the declared models" not in output
    loaded = Config.load(str(config))
    assert loaded.llm.providers["default"].kind == "openrouter"
    assert loaded.llm.models[loaded.llm.tiers["strong"]].model == "vendor/manual"


def test_offline_mode_never_reaches_the_provider(tmp_path, monkeypatch):
    from wenyi_core.llm import selection

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr(
        selection,
        "list_models",
        lambda kind, timeout=20.0, base_url=None: pytest.fail("--offline must not fetch a catalog"),
    )

    result, _ = _invoke(
        tmp_path, {"llm": {"preset": "deepseek"}}, "--provider", "openrouter", "--offline", "--yes"
    )

    assert result.exit_code == 0, result.output


def test_a_live_catalog_is_offered_by_default(tmp_path, monkeypatch):
    from wenyi_core.llm import selection

    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    monkeypatch.setattr(
        selection, "list_models", lambda kind, timeout=20.0, base_url=None: ("vendor/brand-new",)
    )

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "openrouter",
        input="\n\n\ny\n",
        online=True,
    )

    assert result.exit_code == 0, result.output
    assert "vendor/brand-new" in plain_text(result.output)
    loaded = Config.load(str(config))
    assert loaded.llm.models[loaded.llm.tiers["strong"]].model == "vendor/brand-new"


def test_provider_menu_includes_unconfigured_providers_without_declared_models(monkeypatch):
    from io import StringIO

    from rich.console import Console
    from wenyi_cli.commands import model_setup
    from wenyi_core.llm import selection

    offered = []

    def choose(console, label, labels, *, default=0):
        offered.extend(labels)
        return next(index for index, text in enumerate(labels) if text == "OpenAI (openai)")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(model_setup, "select", choose)
    console = Console(file=StringIO())
    selected = model_setup._choose_provider(
        console,
        selection.provider_choices(),
        Config.from_dict({"llm": {"preset": "deepseek"}}),
        provider=None,
        every=False,
    )
    assert selected.kind == "openai"
    assert len(offered) == len(selection.provider_choices())
    assert "Built-in models" not in console.file.getvalue()


def test_online_setup_fetches_after_storing_key_and_uses_selected_endpoint(tmp_path, monkeypatch):
    from wenyi_core.llm import selection

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    calls = []

    def catalog(kind, *, timeout=20.0, base_url=None):
        calls.append((kind, os.environ.get("DEEPSEEK_API_KEY"), base_url))
        return ("endpoint-model",)

    monkeypatch.setattr(selection, "list_models", catalog)
    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "deepseek",
        "--base-url",
        "https://proxy.example/v1",
        input="\ntest-only-key\n\n\ny\n",
        online=True,
    )
    assert result.exit_code == 0, result.output
    assert calls == [("deepseek", "test-only-key", "https://proxy.example/v1")]
    loaded = Config.load(str(config))
    assert {loaded.llm.models[model].model for model in loaded.llm.tiers.values()} == {
        "endpoint-model"
    }


def test_online_failure_does_not_write_preset_models(tmp_path, monkeypatch):
    from wenyi_core.llm import selection

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setattr(selection, "list_models", lambda *a, **kw: ())
    before = {"llm": {"preset": "deepseek"}}
    result, config = _invoke(
        tmp_path,
        before,
        "--provider",
        "deepseek",
        "--yes",
        online=True,
    )
    assert result.exit_code == 2
    assert "No strong model" in plain_text(result.output)
    assert yaml.safe_load(config.read_text()) == before


def test_oauth_success_proceeds_to_live_models_without_endpoint_or_token_echo(
    tmp_path, monkeypatch
):
    from wenyi_core.llm import selection, subscriptions
    from wenyi_core.llm.oauth.credentials import OAuthCredential

    monkeypatch.delenv("WENYI_ANTIGRAVITY_OAUTH", raising=False)
    monkeypatch.setattr(
        subscriptions, "login", lambda *a, **kw: OAuthCredential(access_token="private-test-token")
    )
    monkeypatch.setattr(selection, "list_models", lambda *a, **kw: ("live-model",))
    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "antigravity",
        input="\n\n\ny\n",
        online=True,
    )
    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "Base URL" not in output
    assert "private-test-token" not in output
    assert "authorization completed" in output
    assert "live-model" in output
    assert Config.load(str(config)).llm.models["default_strong"].model == "live-model"


def test_oauth_failure_stops_before_model_selection(tmp_path, monkeypatch):
    from wenyi_core.llm import selection, subscriptions
    from wenyi_core.llm.oauth.credentials import OAuthCredentialError

    monkeypatch.delenv("WENYI_ANTIGRAVITY_OAUTH", raising=False)

    def fail(*args, **kwargs):
        raise OAuthCredentialError("authorization failed")

    monkeypatch.setattr(subscriptions, "login", fail)
    monkeypatch.setattr(selection, "list_models", lambda *a, **kw: pytest.fail("fetched catalog"))
    before = {"llm": {"preset": "deepseek"}}
    result, config = _invoke(tmp_path, before, "--provider", "antigravity", input="\n", online=True)
    assert result.exit_code == 1
    assert yaml.safe_load(config.read_text()) == before


def test_signing_in_stores_the_subscription_credential(tmp_path, monkeypatch):
    from wenyi_core.llm import subscriptions
    from wenyi_core.llm.oauth.credentials import OAuthCredential

    monkeypatch.delenv("WENYI_ANTIGRAVITY_OAUTH", raising=False)
    monkeypatch.setattr(
        subscriptions,
        "login",
        lambda kind, **kwargs: OAuthCredential(
            access_token="token", refresh_token="refresh", email="me@example.com"
        ),
    )

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "antigravity",
        "--yes",
        input="\n",
    )

    assert result.exit_code == 0, result.output
    assert "Sign in with Google Antigravity" in plain_text(result.output)
    stored = read_env_values(tmp_path / ".env")
    assert set(stored) == {"WENYI_ANTIGRAVITY_OAUTH"}
    payload = json.loads(stored["WENYI_ANTIGRAVITY_OAUTH"])
    assert payload["access_token"] == "token"
    assert payload["refresh_token"] == "refresh"
    assert payload["email"] == "me@example.com"
    assert os.environ["WENYI_ANTIGRAVITY_OAUTH"] == stored["WENYI_ANTIGRAVITY_OAUTH"]
    assert Config.load(str(config)).llm.preset == "antigravity"


def test_importing_a_client_credential_is_offered_when_one_exists(tmp_path, monkeypatch):
    from wenyi_cli.commands import model_setup
    from wenyi_core.llm.oauth.credentials import OAuthCredential

    monkeypatch.delenv("WENYI_QWEN_OAUTH", raising=False)
    monkeypatch.setattr(
        model_setup,
        "read_credential_files",
        lambda paths: ([OAuthCredential(access_token="imported")], []),
    )

    result, config = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "qwen-oauth",
        "--strong",
        "qwen3-max",
        "--cheap",
        "qwen3-max",
        "--fast",
        "qwen3-max",
        "--yes",
        input="Import the credential the client wrote\n\n",
    )

    assert result.exit_code == 0, result.output
    assert "Import the credential the client wrote" in plain_text(result.output)
    assert json.loads(read_env_values(tmp_path / ".env")["WENYI_QWEN_OAUTH"]) == {
        "access_token": "imported"
    }
    assert Config.load(str(config)).llm.preset == "qwen-oauth"


def test_a_provider_that_stores_no_credential_rejects_the_flag(tmp_path):
    result, _ = _invoke(
        tmp_path,
        {"llm": {"preset": "deepseek"}},
        "--provider",
        "vertex",
        "--api-key",
        "unused",
        "--yes",
    )

    assert result.exit_code == 2
    assert "accepts no stored credential" in plain_text(result.output)


def test_the_cli_reads_credentials_from_the_env_file_beside_the_config(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"llm": {"preset": "deepseek"}}))
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY='from-file'\n")

    result = CliRunner().invoke(app, ["--config", str(config), "model", "--status", "--json"])

    assert result.exit_code == 0, result.output
    providers = {entry["kind"]: entry for entry in json.loads(result.output)["providers"]}
    assert providers["deepseek"]["configured"] is True
    assert providers["deepseek"]["credential_env"] == "DEEPSEEK_API_KEY"
