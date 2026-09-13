"""Offline previews, explicit conversions and prompt-fixture comparisons."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.llm.migration import convert_config
from trans_novel.llm.providers.fake import FakeProvider
from trans_novel.llm.usage import UsageSample, UsageTracker, convert_usage_ledger, validate_usage


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
    with pytest.raises(ValueError, match="conversion"):
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
    assert "Converted 0" in again.output


def test_comparison_records_each_model_and_its_usage(tmp_path, monkeypatch):
    prompts = tmp_path / "messages.json"
    prompts.write_text(json.dumps([{"role": "user", "content": "A short public test fixture."}]))
    output = tmp_path / "comparison.json"
    config = {
        "llm": {
            "preset": "fake",
            "models": {
                "one": {"provider": "default", "model": "model-one"},
                "two": {"provider": "default", "model": "model-two"},
            },
        }
    }

    def request(self, messages, model, *, json_mode, context):
        context.record_usage(UsageSample(prompt_tokens=5, completion_tokens=2, total_tokens=7))
        return model.model

    monkeypatch.setattr(FakeProvider, "_request", request)
    result = _invoke(
        tmp_path,
        config,
        "compare",
        "--operation",
        "translation.body",
        "--model",
        "one",
        "--model",
        "two",
        "--messages",
        str(prompts),
        "--out",
        str(output),
    )
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert [row["output"] for row in report["results"]] == ["model-one", "model-two"]
    assert report["usage"]["totals"]["total_tokens"] == 14
    assert all(row["usage"]["totals"]["calls"] == 1 for row in report["results"])


@pytest.mark.parametrize("role", [[], {"role": "user"}, None, 1, ("user",)])
def test_comparison_rejects_non_string_roles_before_crashing(tmp_path, role):
    prompts = tmp_path / "messages.json"
    prompts.write_text(json.dumps([{"role": role, "content": "A short public test fixture."}]))
    output = tmp_path / "comparison.json"
    result = _invoke(
        tmp_path,
        {"llm": {"preset": "fake"}},
        "compare",
        "--operation",
        "translation.body",
        "--model",
        "default",
        "--messages",
        str(prompts),
        "--out",
        str(output),
    )
    assert result.exit_code == 1
    assert "Messages must be a nonempty array of role/content objects" in result.output
    assert "Traceback" not in result.output
    assert not output.exists()
