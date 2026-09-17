"""Offline model previews and explicit configuration and usage conversions."""

from __future__ import annotations

import json
import re

import pytest
import yaml
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config
from wenyi_core.llm.migration import convert_config
from wenyi_core.llm.usage import UsageSample, UsageTracker, convert_usage_ledger, validate_usage

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\r")


def plain_text(value: str) -> str:
    """Strip ANSI/control sequences so CLI assertions stay stable under Rich."""
    return _ANSI_RE.sub("", value)


def _invoke(tmp_path, raw, *arguments):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    return CliRunner().invoke(app, ["--config", str(config), "models", *arguments])


def test_preview_needs_no_keys_or_sdk(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("openai.OpenAI", lambda **kw: pytest.fail("preview constructed SDK"))
    result = _invoke(tmp_path, {"llm": {"preset": "deepseek"}}, "list", "--json")
    assert result.exit_code == 0, result.output
    routes = json.loads(result.output)
    assert len(routes) == 17
    assert {route["model"] for route in routes.values()} == {"deepseek-flash"}
    assert routes["synopsis.chapter"]["max_output_tokens"] == 4096
    explained = _invoke(
        tmp_path, {"llm": {"preset": "deepseek"}}, "explain", "--operation", "autofix.verify"
    )
    assert explained.exit_code == 0, explained.output
    assert json.loads(explained.output)["origin"] == "inherits review.verify"


def test_check_respects_disabled_stages(tmp_path, monkeypatch):
    monkeypatch.delenv("UNUSED_REVIEW_KEY", raising=False)
    config = {
        "llm": {
            "preset": "fake",
            "providers": {"remote": {"kind": "openai", "api_key_env": "UNUSED_REVIEW_KEY"}},
            "models": {"editor": {"provider": "remote", "model": "editor"}},
            "routes": {"review.scan": {"model": "editor"}},
        },
        "pipeline": {"review": False},
    }
    checked = _invoke(tmp_path, config, "check", "--for", "translate")
    assert checked.exit_code == 0, checked.output
    failed = _invoke(tmp_path, config, "check", "--for", "review")
    assert failed.exit_code == 1
    assert "UNUSED_REVIEW_KEY" in failed.output
    assert "Traceback" not in failed.output


def test_config_conversion_is_explicit_and_writes_separate_file(tmp_path):
    old = {
        "llm": {"provider": "deepseek", "tiers": {"fast": {"options": {"thinking": False}}}},
        "pipeline": {"review_agent_tier": "cheap"},
    }
    with pytest.raises(ValueError):
        Config.from_dict(old)
    converted = convert_config(old)
    config = Config.from_dict(converted)
    assert config.llm.routes["review.verify"].tier == "cheap"
    assert config.llm.models["fast"].options["thinking"] is False
    assert "review_agent_tier" in old["pipeline"]
    source = tmp_path / "old.yaml"
    source.write_text(yaml.safe_dump(old))
    out = tmp_path / "new.yaml"
    result = _invoke(tmp_path, {}, "migrate-config", str(source), "--out", str(out))
    assert result.exit_code == 0, result.output
    assert Config.load(str(out)).llm == config.llm
    assert yaml.safe_load(source.read_text()) == old
    again = _invoke(tmp_path, {}, "migrate-config", str(source), "--out", str(out))
    assert again.exit_code == 1


@pytest.mark.parametrize("provider", [None, 1, [], {}])
def test_config_conversion_rejects_invalid_provider_types(provider):
    with pytest.raises(ValueError, match="provider must be a non-empty string"):
        convert_config({"llm": {"provider": provider}})


def test_usage_conversion_preserves_totals_and_unknown_identities(tmp_path):
    tracker = UsageTracker()
    tracker.record(
        "cheap", UsageSample(prompt_tokens=7, completion_tokens=3, total_tokens=10), "Reviewer"
    )
    current = tracker.summary()
    old = {key: current[key] for key in ("totals", "by_tier", "by_stage")}
    with pytest.raises(ValueError, match="wenyi models migrate-usage RUN_DIR"):
        validate_usage(old)
    converted = convert_usage_ledger(old)
    assert converted["totals"] == old["totals"]
    assert converted["by_stage"] == old["by_stage"]
    assert converted["by_model"]["unknown"]["total_tokens"] == 10
    run = tmp_path / "target"
    run.mkdir()
    (run / "manifest.json").write_text("{}")
    ledger = run / "usage.json"
    ledger.write_text(json.dumps(old))
    before = ledger.read_bytes()
    result = _invoke(tmp_path, {}, "migrate-usage", str(run))
    assert result.exit_code == 0, result.output
    assert json.loads(ledger.read_text()) == converted
    assert next(run.glob("usage.before-routing-*.json")).read_bytes() == before
    again = _invoke(tmp_path, {}, "migrate-usage", str(run))
    assert again.exit_code == 0
    assert "Converted 0" in plain_text(again.output)


def test_model_comparison_command_is_not_available(tmp_path):
    result = _invoke(tmp_path, {}, "compare")
    assert result.exit_code == 2
    assert "No such command 'compare'" in plain_text(result.output)
