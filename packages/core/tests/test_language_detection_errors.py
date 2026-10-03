"""Auto-language detection reports provider failures without hiding or exposing response bodies."""

import json

import httpx
import pytest
from openai import APIStatusError
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import Config
from wenyi_core.llm.json_parser import JsonParseError
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage


def _inputs(tmp_path):
    source = tmp_path / "sample.txt"
    source.write_text("日本語の原文です。" * 80, encoding="utf-8")
    config = Config.from_dict(
        {
            "language": {"source": "auto", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"book_understanding": False},
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )
    return source, config, FileStorage(str(tmp_path / "run"))


def _failure(status):
    request = httpx.Request("POST", "https://provider.example/chat")
    body = {"error": {"message": "private_response_placeholder"}}
    return APIStatusError(
        "private_response_placeholder",
        response=httpx.Response(status, request=request, json=body),
        body=body,
    )


@pytest.mark.parametrize(
    "status,category", [(402, "insufficient_balance"), (401, "authentication_failed")]
)
def test_provider_failure_is_not_mislabeled_as_unknown_source_language(tmp_path, status, category):
    source, config, store = _inputs(tmp_path)
    error = _failure(status)

    def handler(*_):
        raise error

    client = FakeClient(handler=handler)
    with pytest.raises(ValueError, match=f"HTTP {status}") as caught:
        Orchestrator(config, client=client, storage=store).prepare(str(source))
    assert caught.value.__cause__ is error
    assert "private_response_placeholder" not in str(caught.value)
    assert not store.exists()
    events = store.list_events(event_type="language_detection_failed")
    assert events[-1]["status_code"] == status
    assert events[-1]["error_category"] == category
    assert "private_response_placeholder" not in json.dumps(events)
    assert len(client.calls) == 1


def test_insufficient_balance_cli_message_is_actionable_and_has_no_traceback(tmp_path, monkeypatch):
    source, config, _store = _inputs(tmp_path)
    error = _failure(402)

    def handler(*_):
        raise error

    client = FakeClient(handler=handler)
    monkeypatch.setattr("wenyi_cli.commands.context.CommandContext.load_config", lambda *_: config)
    monkeypatch.setattr("wenyi_core.pipeline.runtime.build_client", lambda *_: client)
    result = CliRunner().invoke(app, ["prepare", str(source)])
    assert result.exit_code == 1
    assert "HTTP 402" in result.output
    assert "balance" in result.output.lower()
    assert "provider" in result.output.lower()
    assert "Traceback" not in result.output
    assert "private_response_placeholder" not in result.output
    assert "such as ja/en" not in result.output


def test_invalid_json_has_its_own_safe_diagnostic(tmp_path):
    source, config, store = _inputs(tmp_path)

    def handler(*_):
        raise JsonParseError("private_response_placeholder")

    with pytest.raises(ValueError, match="not valid JSON") as caught:
        Orchestrator(config, client=FakeClient(handler=handler), storage=store).prepare(str(source))
    assert "private_response_placeholder" not in str(caught.value)
    event = store.list_events(event_type="language_detection_failed")[-1]
    assert event["error_category"] == "invalid_json"
    assert "status_code" not in event


def test_invalid_language_result_still_requests_an_explicit_supported_language(tmp_path):
    source, config, store = _inputs(tmp_path)
    client = FakeClient(handler=lambda *_: '{"language":"unknown"}')
    with pytest.raises(ValueError, match="(?i)set language.source"):
        Orchestrator(config, client=client, storage=store).prepare(str(source))
    event = store.list_events(event_type="language_detection_failed")[-1]
    assert event["reason"] == "unsupported_language_result"
    assert "status_code" not in event
