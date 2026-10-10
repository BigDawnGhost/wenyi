"""Offline checks separating transport limits from terminology reference validation."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from wenyi_core.agents.terminology import TerminologyAgent, TerminologyReferenceError
from wenyi_core.config import Config
from wenyi_core.glossary.evidence_models import SourcePassage
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.llm.retrying import TruncatedResponseError
from wenyi_core.llm.router import RoutedLLMClient


def response(content, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
        ],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10),
    )


def mocked_client(monkeypatch, *, kind="deepseek", explicit=None, retries=1):
    config = Config.from_dict({"llm": {"preset": "deepseek"}})
    raw = config.llm.model_dump()
    raw["providers"]["default"]["kind"] = kind
    raw["providers"]["default"]["max_retries"] = retries
    if kind == "openai-compatible":
        raw["providers"]["default"]["base_url"] = "https://offline.invalid"
        for model in raw["models"].values():
            model["options"] = {"thinking": False}
    if explicit is not None:
        for model in raw["models"].values():
            model["max_output_tokens"] = explicit
    config = Config.from_dict({"llm": raw})
    client = RoutedLLMClient(config.llm)
    sdk = MagicMock()
    client.adapter("default")._client = sdk
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda delay: None)
    return config, client, sdk.chat.completions.create


@pytest.mark.parametrize(
    ("operation", "limit"),
    [
        ("terminology.discover", None),
        ("terminology.evidence", None),
        ("terminology.merge", None),
        ("synopsis.chapter", 8192),
        ("synopsis.book", 8192),
    ],
)
def test_plain_deepseek_preset_wire_output_limits(monkeypatch, operation, limit):
    _, client, create = mocked_client(monkeypatch)
    create.return_value = response("{}")
    assert client.routes[operation].max_output_tokens == limit
    assert client.complete([], operation=operation, json_mode=True) == "{}"
    kwargs = create.call_args.kwargs
    if limit is None:
        assert "max_tokens" not in kwargs
    else:
        assert kwargs["max_tokens"] == limit
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert kwargs["reasoning_effort"] == "high"


@pytest.mark.parametrize("operation", ["terminology.evidence", "synopsis.chapter", "synopsis.book"])
def test_explicit_profile_limit_overrides_operation_and_call_hints(monkeypatch, operation):
    _, client, create = mocked_client(monkeypatch, explicit=4096)
    create.return_value = response("{}")
    client.complete([], operation=operation, max_tokens=16000)
    assert client.routes[operation].max_output_tokens == 4096
    assert create.call_args.kwargs["max_tokens"] == 4096


@pytest.mark.parametrize("kind", ["deepseek", "openai-compatible"])
@pytest.mark.parametrize("content", ["not JSON", '{"note":"Complete but missing citations"}'])
def test_length_precedes_json_and_reference_validation_and_counts_usage(monkeypatch, kind, content):
    config, client, create = mocked_client(monkeypatch, kind=kind)
    create.return_value = response(content, "length")
    worker = TerminologyAgent(client, config)
    with pytest.raises(TruncatedResponseError):
        worker.enrich(
            GlossaryTerm("Ann", "安"),
            [SourcePassage(reference="r1", chapter=0, segment=0, source="Ann arrived.")],
        )
    assert create.call_count == 1
    assert client.usage_summary()["totals"]["calls"] == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 10


@pytest.mark.parametrize("operation", ["synopsis.chapter", "synopsis.book"])
def test_synopsis_length_retries_and_counts_both_responses(monkeypatch, operation):
    _, client, create = mocked_client(monkeypatch)
    create.side_effect = [response("Partial", "length"), response("Complete summary.")]
    events = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))
    assert client.complete([], operation=operation) == "Complete summary."
    assert create.call_count == 2
    assert client.usage_summary()["totals"]["total_tokens"] == 20
    starts = [event for event in events if event["event"] == "llm_request_started"]
    assert [event["attempt"] for event in starts] == [1, 2]
    assert len({event["call_id"] for event in starts}) == 1


@pytest.mark.parametrize("refs", [[], ["outside"]])
def test_completed_evidence_reference_error_is_not_transport_truncation(monkeypatch, refs):
    config, client, create = mocked_client(monkeypatch)
    create.return_value = response(json.dumps({"note": "Ann arrived.", "evidence_refs": refs}))
    worker = TerminologyAgent(client, config)
    with pytest.raises(TerminologyReferenceError) as failure:
        worker.enrich(
            GlossaryTerm("Ann", "安"),
            [SourcePassage(reference="r1", chapter=0, segment=0, source="Ann arrived.")],
        )
    assert not isinstance(failure.value, TruncatedResponseError)
    assert failure.value.reason == ("outside_input" if refs else "missing")
    assert create.call_count == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 10
