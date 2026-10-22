"""Offline contracts for live model connectivity probes using injected SDK responses."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
import yaml
from openai import APIConnectionError, APIStatusError
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.connectivity import (
    PROBE_OUTPUT_TOKENS,
    probe_connections,
    probe_profiles,
    probe_targets,
)
from wenyi_core.llm.providers._openai_compatible import OpenAICompatibleBaseClient
from wenyi_core.llm.router import RoutedLLMClient

SECRET = "fake-probe-secret-not-a-real-key"
_NO_THINKING = {"thinking": False}


def _config(**extra) -> LLMConfig:
    return LLMConfig.model_validate(
        {
            "providers": {
                "main": {"kind": "openai", "api_key_env": "PROBE_MAIN_KEY", "max_retries": 4},
                "backup": {
                    "kind": "openai-compatible",
                    "base_url": "https://backup.invalid/v1",
                    "api_key_env": "PROBE_BACKUP_KEY",
                },
            },
            "models": {
                "strong": {"provider": "main", "model": "big", "options": _NO_THINKING},
                "small": {"provider": "main", "model": "small", "options": _NO_THINKING},
                "spare": {"provider": "backup", "model": "spare"},
            },
            "tiers": {"strong": "strong", "cheap": "small", "fast": "small"},
            **extra,
        }
    )


def _response(content="OK", finish_reason="stop"):
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))
        ],
    )


def _status_error(status: int) -> APIStatusError:
    request = httpx.Request(
        "POST", "https://example.invalid", headers={"Authorization": f"Bearer {SECRET}"}
    )
    return APIStatusError(
        f"Bearer {SECRET}",
        response=httpx.Response(status, request=request),
        body={"key": SECRET},
    )


@pytest.fixture
def sdk(monkeypatch):
    """Replace the OpenAI SDK client; ``behaviors`` maps model names to replies or errors."""
    calls: list[dict] = []
    behaviors: dict[str, object] = {}
    clients: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        behavior = behaviors.get(kwargs["model"], _response())
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    def ensure(self):
        clients.append({"timeout": self.cfg.timeout, "max_retries": self.cfg.max_retries})
        self.validate_credentials()
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(OpenAICompatibleBaseClient, "_ensure_client", ensure)
    monkeypatch.setenv("PROBE_MAIN_KEY", SECRET)
    monkeypatch.setenv("PROBE_BACKUP_KEY", SECRET)
    return SimpleNamespace(calls=calls, behaviors=behaviors, clients=clients)


def test_targets_deduplicate_profiles_and_include_fallbacks():
    config = _config(routes={"translation.body": {"tier": "strong", "fallbacks": ["spare"]}})
    targets = probe_targets(config, ("translation.body", "translation.title", "glossary.extract"))
    assert list(targets) == ["strong", "spare", "small"]
    assert targets["strong"] == ("translation.body", "translation.title")
    assert targets["spare"] == ("translation.body",)
    assert targets["small"] == ("glossary.extract",)


def test_probe_sends_one_minimal_request_per_profile(sdk):
    results = probe_connections(_config(), ("translation.body", "glossary.extract"), timeout=5)
    assert [result.profile for result in results] == ["strong", "small"]
    assert all(result.ok and result.category == "ok" for result in results)
    assert all(result.latency_ms is not None for result in results)
    assert [call["model"] for call in sdk.calls] == ["big", "small"]
    assert all(call["max_completion_tokens"] == PROBE_OUTPUT_TOKENS for call in sdk.calls)
    # Probes never retry and never wait the full workflow timeout.
    assert sdk.clients == [{"timeout": 5, "max_retries": 0}] * 2


@pytest.mark.parametrize(
    "status, category", [(401, "authentication_failed"), (404, "model_or_endpoint_not_found")]
)
def test_http_failures_are_classified_without_leaking_secrets(sdk, status, category):
    sdk.behaviors["big"] = _status_error(status)
    [result] = probe_connections(_config(), ("translation.body",))
    assert not result.ok
    assert result.category == category
    assert result.status_code == status
    assert len(sdk.calls) == 1
    assert SECRET not in json.dumps(result.describe())


def test_transient_failures_are_not_retried(sdk):
    sdk.behaviors["big"] = _status_error(503)
    [result] = probe_connections(_config(), ("translation.body",))
    assert result.category == "provider_unavailable"
    assert len(sdk.calls) == 1


def test_connection_errors_are_reported(sdk):
    sdk.behaviors["big"] = APIConnectionError(request=httpx.Request("POST", "https://x.invalid"))
    [result] = probe_connections(_config(), ("translation.body",))
    assert (result.ok, result.category) == (False, "connection_failed")


def test_truncated_or_empty_replies_still_prove_connectivity(sdk):
    sdk.behaviors["big"] = _response("O", finish_reason="length")
    sdk.behaviors["small"] = _response("")
    results = probe_connections(_config(), ("translation.body", "glossary.extract"))
    assert [(result.ok, result.category) for result in results] == [
        (True, "reachable"),
        (True, "reachable"),
    ]


def test_missing_credentials_skip_the_request(sdk, monkeypatch):
    monkeypatch.delenv("PROBE_MAIN_KEY")
    [result] = probe_connections(_config(), ("translation.body",))
    assert (result.ok, result.category) == (False, "missing_credential")
    assert "PROBE_MAIN_KEY" in result.message
    assert sdk.calls == []


def test_injected_credentials_are_used_per_connection(sdk, monkeypatch):
    monkeypatch.delenv("PROBE_MAIN_KEY")
    [result] = probe_connections(
        _config(), ("translation.body",), credentials={"main": "desktop-key"}
    )
    assert result.ok
    [missing] = probe_connections(_config(), ("translation.body",), credentials={})
    assert missing.category == "missing_credential"


def test_thinking_profiles_keep_their_minimum_output(sdk):
    config = _config(
        models={
            "strong": {"provider": "main", "model": "big", "options": {"thinking": True}},
            "small": {"provider": "main", "model": "small", "options": _NO_THINKING},
            "spare": {"provider": "backup", "model": "spare"},
        }
    )
    probe_connections(config, ("translation.body",))
    assert sdk.calls[0]["max_completion_tokens"] == 4096


def test_profile_probes_cover_unrouted_profiles_in_registry_order(sdk):
    results = probe_profiles(_config())
    assert [result.profile for result in results] == ["strong", "small", "spare"]
    assert results[2].operations == ()
    assert results[2].endpoint == "https://backup.invalid/v1"
    assert [call["model"] for call in sdk.calls] == ["big", "small", "spare"]
    assert [result.profile for result in probe_profiles(_config(), ["spare"])] == ["spare"]
    with pytest.raises(ValueError, match="Unknown model profile"):
        probe_profiles(_config(), ["missing"])


def test_routed_client_probes_with_its_credential_snapshot(sdk, monkeypatch):
    monkeypatch.delenv("PROBE_MAIN_KEY")
    client = RoutedLLMClient(_config(), credentials={"main": "snapshot-key", "backup": None})
    results = {result.profile: result for result in client.probe_profiles(timeout=3)}
    assert results["strong"].ok and results["small"].ok
    assert results["spare"].category == "missing_credential"
    assert sdk.clients == [{"timeout": 3, "max_retries": 0}] * 2


def test_invalid_timeout_is_rejected():
    with pytest.raises(ValueError, match="timeout"):
        probe_connections(_config(), ("translation.body",), timeout=0)


def _invoke(tmp_path, raw, *arguments):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return CliRunner().invoke(app, ["--config", str(path), "models", "ping", *arguments])


def test_cli_ping_reports_results_and_exit_code(tmp_path, sdk):
    raw = {"llm": _config().model_dump(mode="json", exclude_none=True)}
    passed = _invoke(tmp_path, raw, "--for", "srt", "--json")
    assert passed.exit_code == 0, passed.output
    assert [item["profile"] for item in json.loads(passed.output)] == ["strong"]

    sdk.behaviors["big"] = _status_error(401)
    failed = _invoke(tmp_path, raw, "--for", "srt", "--json")
    assert failed.exit_code == 1
    assert json.loads(failed.output)[0]["category"] == "authentication_failed"
    table = _invoke(tmp_path, raw, "--for", "srt")
    assert table.exit_code == 1
    assert "1 of 1 model profiles unreachable" in table.output
    for output in (failed.output, table.output):
        assert SECRET not in output
        assert "Traceback" not in output


def test_cli_ping_rejects_unknown_workflow(tmp_path):
    result = _invoke(tmp_path, {"llm": {"preset": "fake"}}, "--for", "publish")
    assert result.exit_code == 1
    assert "Unknown model workflow" in result.output
