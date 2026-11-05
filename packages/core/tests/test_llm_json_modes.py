"""Offline JSON negotiation must validate replies without changing saved profiles."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from types import SimpleNamespace

import httpx
import pytest
import yaml
from openai import APIStatusError
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.connectivity import probe_profiles
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.providers._openai_compatible import OpenAICompatibleBaseClient
from wenyi_core.llm.router import RoutedLLMClient

VALUE = {"probe": "wenyi", "items": [1, 2]}
FORMAT_ERROR = "'response_format.type' must be 'json_schema' or 'text'"


def config(mode=None, kind="openai-compatible", max_retries=0):
    options = {} if mode is None else {"json_output_mode": mode}
    return LLMConfig.model_validate(
        {
            "providers": {
                "local": {
                    "kind": kind,
                    "base_url": "https://local.invalid/v1",
                    "max_retries": max_retries,
                }
            },
            "models": {"model": {"provider": "local", "model": "example", "options": options}},
            "tiers": {tier: "model" for tier in ("strong", "cheap", "fast")},
        }
    )


def response(content, finish_reason="stop"):
    return SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))
        ],
    )


def error(status, message):
    return APIStatusError(
        "Untrusted provider details",
        response=httpx.Response(status, request=httpx.Request("POST", "https://local.invalid")),
        body={"error": message},
    )


@pytest.fixture
def sdk(monkeypatch):
    calls = []
    state = SimpleNamespace(
        reject_object=True,
        reply=json.dumps(VALUE),
        failure=None,
        finish="stop",
        usage=None,
        on_call=lambda: None,
    )

    def create(**kwargs):
        calls.append(kwargs)
        state.on_call()
        output = kwargs.get("response_format")
        if output is None:
            return response("OK")
        if state.failure:
            raise state.failure
        if output["type"] == "json_object" and state.reject_object:
            raise error(400, FORMAT_ERROR)
        result = response(state.reply, state.finish)
        result.usage = state.usage
        return result

    monkeypatch.setattr(
        OpenAICompatibleBaseClient,
        "_ensure_client",
        lambda self: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    return calls, state


def test_runtime_reuses_negotiated_mode_across_operations_without_changing_config(sdk):
    calls, state = sdk
    state.usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    original = config()
    before = original.model_dump()
    client = RoutedLLMClient(original, credentials={"local": None})
    events = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))

    assert client.complete_json([], operation="analysis.style") == VALUE
    assert client.complete_json([], operation="translation.body") == VALUE
    assert client.complete([], operation="synopsis.chapter") == "OK"
    assert [call.get("response_format", {}).get("type") for call in calls] == [
        "json_object",
        "json_schema",
        "json_schema",
        None,
    ]
    assert original.model_dump() == client.config.model_dump() == before
    assert client.usage_summary()["totals"]["calls"] == 2
    assert client.usage_summary()["totals"]["total_tokens"] == 10
    starts = [event for event in events if event["event"] == "llm_request_started"]
    assert [event["attempt"] for event in starts] == [1, 2, 1, 1]
    modes = [event for event in events if event["event"] == "llm_json_mode_selected"]
    assert [event["json_output_mode"] for event in modes] == [
        "json_object",
        "json_schema",
        "json_schema",
    ]
    assert [event["json_mode_source"] for event in modes] == ["configured", "fallback", "cached"]
    assert FORMAT_ERROR not in json.dumps(events)


@pytest.mark.parametrize("difference", ["model", "options", "connection", "client"])
def test_runtime_mode_cache_is_scoped_to_connection_and_model_configuration(sdk, difference):
    calls, _ = sdk
    settings = config().model_dump()
    settings["models"]["other"] = {
        **settings["models"]["model"],
        "options": {"thinking": True},
    }
    if difference != "options":
        settings["models"]["other"] = dict(settings["models"]["model"])
    if difference == "model":
        settings["models"]["other"]["model"] = "another"
    elif difference == "connection":
        settings["providers"]["other"] = dict(settings["providers"]["local"])
        settings["models"]["other"]["provider"] = "other"
    settings["routes"] = {"translation.body": {"model": "other"}}
    original = LLMConfig.model_validate(settings)
    client = RoutedLLMClient(original)
    assert client.complete_json([], operation="analysis.style") == VALUE
    if difference == "client":
        client = RoutedLLMClient(original)
    assert client.complete_json([], operation="translation.body") == VALUE
    assert [call["response_format"]["type"] for call in calls] == [
        "json_object",
        "json_schema",
        "json_object",
        "json_schema",
    ]


@pytest.mark.parametrize(
    "status,message",
    [
        (400, "Context length exceeded"),
        (401, FORMAT_ERROR),
        (429, FORMAT_ERROR),
        (503, FORMAT_ERROR),
    ],
)
def test_runtime_does_not_switch_modes_for_unrelated_errors(sdk, status, message):
    calls, state = sdk
    state.failure = error(status, message)
    with pytest.raises(APIStatusError):
        RoutedLLMClient(config()).complete_json([], operation="analysis.style")
    assert [call["response_format"]["type"] for call in calls] == ["json_object"]


@pytest.mark.parametrize("payload", ["not JSON", "[]", '```json\n{"ok":true}\n```'])
def test_runtime_does_not_remember_invalid_json_as_a_working_mode(sdk, payload):
    calls, state = sdk
    state.reply = payload
    client = RoutedLLMClient(config())
    assert client.complete([], operation="analysis.style", json_mode=True) == payload
    state.reply = json.dumps(VALUE)
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert [call["response_format"]["type"] for call in calls] == [
        "json_object",
        "json_schema",
        "json_object",
        "json_schema",
    ]


def test_runtime_switches_at_most_once_and_does_not_cache_a_failed_alternate(sdk):
    calls, state = sdk
    state.failure = error(422, FORMAT_ERROR)
    client = RoutedLLMClient(config())
    with pytest.raises(APIStatusError):
        client.complete_json([], operation="analysis.style")
    assert len(calls) == 2
    state.failure = None
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert [call["response_format"]["type"] for call in calls] == [
        "json_object",
        "json_schema",
        "json_object",
        "json_schema",
    ]


def test_runtime_rechecks_a_cached_mode_when_the_endpoint_changes(sdk):
    calls, state = sdk
    client = RoutedLLMClient(config())
    assert client.complete_json([], operation="analysis.style") == VALUE
    state.reject_object = False
    state.on_call = lambda: setattr(
        state,
        "failure",
        error(400, "'response_format.type' only supports 'json_object'")
        if calls[-1]["response_format"]["type"] == "json_schema"
        else None,
    )
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert [call["response_format"]["type"] for call in calls] == [
        "json_object",
        "json_schema",
        "json_schema",
        "json_object",
        "json_object",
    ]


@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("transient_before_switch", [True, False])
def test_mode_switch_preserves_one_shared_transient_retry_budget(
    sdk, monkeypatch, recover, transient_before_switch
):
    calls, state = sdk
    client = RoutedLLMClient(config(max_retries=1))
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda delay: None)
    transient_attempt = 1 if transient_before_switch else 2
    state.on_call = lambda: setattr(
        state,
        "failure",
        error(503, "Unavailable")
        if (len(calls) == transient_attempt or (not recover and len(calls) == 3))
        else None,
    )
    if recover:
        assert client.complete_json([], operation="analysis.style") == VALUE
    else:
        with pytest.raises(APIStatusError):
            client.complete_json([], operation="analysis.style")
    assert [call["response_format"]["type"] for call in calls] == [
        "json_object",
        "json_object" if transient_before_switch else "json_schema",
        "json_schema",
    ]


@pytest.mark.parametrize("stop", ["cancel", "budget"])
def test_mode_switch_obeys_cancellation_and_request_budget(sdk, stop):
    calls, state = sdk
    settings = config().model_dump()
    settings["budget"] = {"max_requests": 1} if stop == "budget" else {}
    client = RoutedLLMClient(LLMConfig.model_validate(settings))
    if stop == "cancel":
        state.on_call = client.cancel
    with pytest.raises(RequestCancelled if stop == "cancel" else RequestStopped):
        client.complete_json([], operation="analysis.style")
    assert len(calls) == 1


def test_parallel_negotiation_does_not_hold_a_cache_lock_during_requests(sdk):
    calls, state = sdk
    client = RoutedLLMClient(config())
    barrier, thread = Barrier(2), local()

    def synchronize_first_call():
        if not getattr(thread, "started", False):
            thread.started = True
            barrier.wait(timeout=5)

    state.on_call = synchronize_first_call
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: client.complete_json([], operation="analysis.style"),
                range(2),
            )
        )
    assert results == [VALUE, VALUE]
    assert len(calls) == 4
    state.on_call = lambda: None
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert calls[-1]["response_format"]["type"] == "json_schema"
    assert len(calls) == 5


def test_runtime_native_provider_does_not_use_compatible_negotiation(sdk):
    calls, _ = sdk
    client = RoutedLLMClient(config(kind="openai"))
    with pytest.raises(APIStatusError):
        client.complete_json([], operation="analysis.style")
    assert [call["response_format"]["type"] for call in calls] == ["json_object"]


def test_runtime_schema_preference_can_negotiate_and_reuse_json_object(sdk):
    calls, state = sdk
    state.reject_object = False
    state.on_call = lambda: setattr(
        state,
        "failure",
        error(422, "response_format json_schema is not supported")
        if calls[-1]["response_format"]["type"] == "json_schema"
        else None,
    )
    client = RoutedLLMClient(config("json_schema"))
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert [call["response_format"]["type"] for call in calls] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


def test_json_probe_negotiates_schema_without_mutating_configuration(sdk):
    calls, _ = sdk
    original = config()
    before = original.model_dump()
    [result] = probe_profiles(original, detect_json=True)
    assert result.ok
    assert result.json_mode == "json_schema"
    assert result.json_category == "ok"
    assert [call.get("response_format", {}).get("type") for call in calls] == [
        None,
        "json_object",
        "json_schema",
    ]
    assert calls[-1]["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert original.model_dump() == before


def test_json_probe_keeps_working_json_object(sdk):
    calls, state = sdk
    state.reject_object = False
    [result] = probe_profiles(config(), detect_json=True)
    assert result.json_mode == "json_object"
    assert result.json_category == "ok"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"probe": "wenyi"},
        {"probe": "wenyi", "items": [True, 2]},
        {"probe": "wenyi", "items": [1, 2], "extra": "unexpected"},
        "not JSON",
        '```json\n{"probe":"wenyi","items":[1,2]}\n```',
    ],
)
def test_json_probe_rejects_incomplete_or_repaired_payloads(sdk, body):
    calls, state = sdk
    state.reject_object = False
    state.reply = body if isinstance(body, str) else json.dumps(body)
    [result] = probe_profiles(config(), detect_json=True)
    assert result.ok
    assert result.json_mode is None
    assert result.json_category == "invalid_json_response"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "status,message",
    [
        (400, "Context length exceeded"),
        (401, FORMAT_ERROR),
        (429, FORMAT_ERROR),
        (503, FORMAT_ERROR),
    ],
)
def test_json_probe_does_not_negotiate_unrelated_failures(sdk, status, message):
    calls, state = sdk
    state.failure = error(status, message)
    [result] = probe_profiles(config(), detect_json=True)
    assert result.ok
    assert result.json_mode is None
    assert result.json_status_code == status
    assert len(calls) == 2
    assert "Untrusted" not in json.dumps(result.describe())
    assert message not in json.dumps(result.describe())


@pytest.mark.parametrize("content,finish", [("", "stop"), ('{"probe":', "length")])
def test_empty_or_truncated_json_is_not_a_success(sdk, content, finish):
    _, state = sdk
    state.reject_object = False
    state.reply, state.finish = content, finish
    [result] = probe_profiles(config(), detect_json=True)
    assert result.ok
    assert result.json_mode is None
    assert result.json_category == "invalid_json_response"


def test_saved_schema_mode_is_used_only_for_json_requests(sdk):
    calls, _ = sdk
    client = RoutedLLMClient(config("json_schema"))
    client.complete([{"role": "user", "content": "Summary."}], operation="synopsis.chapter")
    assert "response_format" not in calls[-1]
    assert (
        client.complete_json(
            [{"role": "user", "content": "Return JSON."}], operation="analysis.style"
        )
        == VALUE
    )
    assert calls[-1]["response_format"]["type"] == "json_schema"
    [result] = client.probe_profiles(detect_json=True)
    assert result.json_mode == "json_schema"
    assert len(calls) == 4


def test_json_mode_rejects_unknown_values_and_does_not_unlock_raw_overrides():
    with pytest.raises(ValueError):
        config("automatic-guess")
    original = config().model_dump()
    original["models"]["model"]["options"] = {
        "request_overrides": {"response_format": {"type": "text"}}
    }
    with pytest.raises(ValueError, match="Reserved model option"):
        LLMConfig.model_validate(original)


def test_native_provider_is_tested_without_compatible_options(sdk):
    calls, state = sdk
    state.reject_object = False
    [result] = probe_profiles(
        config(kind="openai"), credentials={"local": "test-key"}, detect_json=True
    )
    assert result.json_mode == "native"
    assert result.json_category == "ok"
    assert len(calls) == 2


def test_json_probe_respects_the_remaining_profile_deadline(sdk, monkeypatch):
    calls, state = sdk
    clock = [0.0]
    monkeypatch.setattr(
        "wenyi_core.llm.connectivity.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    state.on_call = lambda: clock.__setitem__(0, 6.0)
    [result] = probe_profiles(config(), timeout=5, detect_json=True)
    assert result.ok
    assert result.json_category == "timeout"
    assert len(calls) == 1


def test_cli_json_detection_is_opt_in_and_does_not_write_configuration(tmp_path, sdk):
    path = tmp_path / "config.yaml"
    text = yaml.safe_dump({"llm": config().model_dump(mode="json")})
    path.write_text(text)
    result = CliRunner().invoke(
        app, ["--config", str(path), "models", "ping", "--detect-json", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["json_mode"] == "json_schema"
    assert path.read_text() == text


def test_real_sdk_serializes_schema_and_probe_connections_are_closed(monkeypatch):
    from openai import OpenAI

    bodies, clients = [], []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("response_format", {}).get("type") == "json_object":
            return httpx.Response(400, json={"error": {"message": FORMAT_ERROR}})
        content = json.dumps(VALUE) if "response_format" in body else "OK"
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "example",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )

    def ensure(self):
        if self._client is None:
            self._client = OpenAI(
                api_key="no-key",
                base_url=self.base_url,
                max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond)),
            )
            clients.append(self._client)
        return self._client

    monkeypatch.setattr(OpenAICompatibleBaseClient, "_ensure_client", ensure)
    [result] = probe_profiles(config(), detect_json=True)
    assert result.json_mode == "json_schema"
    assert [body.get("response_format", {}).get("type") for body in bodies] == [
        None,
        "json_object",
        "json_schema",
    ]
    assert all(client.is_closed() for client in clients)
