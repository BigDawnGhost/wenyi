"""Runtime JSON compatibility must not introduce configuration or probe preferences."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.providers._openai_compatible import OpenAICompatibleBaseClient
from wenyi_core.llm.retrying import EmptyResponseError, TruncatedResponseError
from wenyi_core.llm.router import RoutedLLMClient

VALUE = {"translations": ["Translated paragraph."]}
FORMAT_ERROR = "'response_format.type' must be 'json_schema' or 'text'"


def config(kind="openai-compatible", max_retries=0):
    return LLMConfig.model_validate(
        {
            "providers": {
                "local": {
                    "kind": kind,
                    "base_url": "https://local.invalid/v1",
                    "max_retries": max_retries,
                }
            },
            "models": {"model": {"provider": "local", "model": "example"}},
            "tiers": {tier: "model" for tier in ("strong", "cheap", "fast")},
        }
    )


def error(status, message):
    return APIStatusError(
        "Untrusted provider details",
        response=httpx.Response(status, request=httpx.Request("POST", "https://local.invalid")),
        body={"error": message},
    )


def modes(calls):
    return [call.get("response_format", {}).get("type") for call in calls]


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
        mode = kwargs.get("response_format", {}).get("type")
        if mode is not None and state.failure:
            raise state.failure
        if mode == "json_object" and state.reject_object:
            raise error(400, FORMAT_ERROR)
        return SimpleNamespace(
            usage=state.usage if mode else None,
            choices=[
                SimpleNamespace(
                    finish_reason=state.finish if mode else "stop",
                    message=SimpleNamespace(content=state.reply if mode else "OK"),
                )
            ],
        )

    monkeypatch.setattr(
        OpenAICompatibleBaseClient,
        "_ensure_client",
        lambda self: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    return calls, state


def test_runtime_reuses_schema_across_translation_steps_without_changing_config(sdk):
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
    assert modes(calls) == ["json_object", "json_schema", "json_schema", None]
    assert original.model_dump() == client.config.model_dump() == before
    assert client.usage_summary()["totals"]["calls"] == 2
    assert client.usage_summary()["totals"]["total_tokens"] == 10
    starts = [event for event in events if event["event"] == "llm_request_started"]
    assert [event["attempt"] for event in starts] == [1, 2, 1, 1]
    selected = [event for event in events if event["event"] == "llm_json_mode_selected"]
    assert [event["json_output_mode"] for event in selected] == modes(calls)[:3]
    assert [event["json_mode_source"] for event in selected] == ["default", "fallback", "cached"]
    assert FORMAT_ERROR not in json.dumps(events)


def test_successful_json_object_does_not_trigger_a_schema_request(sdk):
    calls, state = sdk
    state.reject_object = False
    client = RoutedLLMClient(config())
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert client.complete_json([], operation="translation.body") == VALUE
    assert modes(calls) == ["json_object", "json_object"]


def test_connectivity_stays_plain_text_and_does_not_prime_runtime_modes(sdk):
    calls, _ = sdk
    client = RoutedLLMClient(config())
    [result] = client.probe_profiles()
    assert result.ok
    assert not any(key.startswith("json_") for key in result.describe())
    assert modes(calls) == [None]
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert modes(calls) == [None, "json_object", "json_schema"]


def test_json_output_mode_is_not_a_configuration_option():
    settings = config().model_dump()
    settings["models"]["model"]["options"] = {"json_output_mode": "json_schema"}
    with pytest.raises(ValueError):
        LLMConfig.model_validate(settings)


@pytest.mark.parametrize("difference", ["model", "options", "connection", "client"])
def test_runtime_cache_is_scoped_to_connection_and_model_configuration(sdk, difference):
    calls, _ = sdk
    settings = config().model_dump()
    settings["models"]["other"] = dict(settings["models"]["model"])
    if difference == "model":
        settings["models"]["other"]["model"] = "another"
    elif difference == "options":
        settings["models"]["other"]["options"] = {"thinking": True}
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
    assert modes(calls) == ["json_object", "json_schema", "json_object", "json_schema"]


@pytest.mark.parametrize(
    "status,message",
    [
        (400, "Context length exceeded"),
        (400, "response_format json_schema must contain a valid schema"),
        (401, FORMAT_ERROR),
        (429, FORMAT_ERROR),
        (503, FORMAT_ERROR),
    ],
)
def test_unrelated_failures_do_not_switch_formats(sdk, status, message):
    calls, state = sdk
    state.failure = error(status, message)
    with pytest.raises(APIStatusError):
        RoutedLLMClient(config()).complete_json([], operation="analysis.style")
    assert modes(calls) == ["json_object"]


@pytest.mark.parametrize(
    "content,finish,expected",
    [("", "stop", EmptyResponseError), ('{"translations":', "length", TruncatedResponseError)],
)
def test_incomplete_responses_do_not_switch_formats(sdk, content, finish, expected):
    calls, state = sdk
    state.reject_object = False
    state.reply, state.finish = content, finish
    with pytest.raises(expected):
        RoutedLLMClient(config()).complete_json([], operation="translation.body")
    assert modes(calls) == ["json_object"]


@pytest.mark.parametrize("payload", ["not JSON", "[]", '```json\n{"ok":true}\n```'])
def test_invalid_json_does_not_confirm_a_working_format(sdk, payload):
    calls, state = sdk
    state.reply = payload
    client = RoutedLLMClient(config())
    assert client.complete([], operation="analysis.style", json_mode=True) == payload
    state.reply = json.dumps(VALUE)
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert modes(calls) == ["json_object", "json_schema", "json_object", "json_schema"]


def test_both_formats_rejected_fail_without_looping_or_caching(sdk):
    calls, state = sdk
    state.failure = error(422, FORMAT_ERROR)
    client = RoutedLLMClient(config())
    with pytest.raises(APIStatusError):
        client.complete_json([], operation="analysis.style")
    assert len(calls) == 2
    state.failure = None
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert modes(calls) == ["json_object", "json_schema", "json_object", "json_schema"]


def test_rejected_cached_schema_can_return_to_json_object(sdk):
    calls, state = sdk
    client = RoutedLLMClient(config())
    assert client.complete_json([], operation="analysis.style") == VALUE
    state.reject_object = False
    state.on_call = lambda: setattr(
        state,
        "failure",
        error(400, "'response_format.type' only supports 'json_object'")
        if modes(calls)[-1] == "json_schema"
        else None,
    )
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert modes(calls) == [
        "json_object",
        "json_schema",
        "json_schema",
        "json_object",
        "json_object",
    ]


@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("transient_before_switch", [True, False])
def test_negotiation_keeps_one_transient_retry_budget(
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
    assert modes(calls) == [
        "json_object",
        "json_object" if transient_before_switch else "json_schema",
        "json_schema",
    ]


@pytest.mark.parametrize("stop", ["cancel", "budget"])
def test_each_wire_attempt_obeys_cancellation_and_budget(sdk, stop):
    calls, state = sdk
    settings = config().model_dump()
    settings["budget"] = {"max_requests": 1} if stop == "budget" else {}
    client = RoutedLLMClient(LLMConfig.model_validate(settings))
    if stop == "cancel":
        state.on_call = client.cancel
    with pytest.raises(RequestCancelled if stop == "cancel" else RequestStopped):
        client.complete_json([], operation="analysis.style")
    assert len(calls) == 1


def test_parallel_calls_do_not_hold_the_cache_lock_during_requests(sdk):
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
            pool.map(lambda _: client.complete_json([], operation="analysis.style"), range(2))
        )
    assert results == [VALUE, VALUE]
    assert len(calls) == 4
    state.on_call = lambda: None
    assert client.complete_json([], operation="analysis.style") == VALUE
    assert modes(calls)[-1] == "json_schema"
    assert len(calls) == 5


def test_native_openai_does_not_negotiate_compatible_formats(sdk):
    calls, _ = sdk
    with pytest.raises(APIStatusError):
        RoutedLLMClient(config(kind="openai")).complete_json([], operation="analysis.style")
    assert modes(calls) == ["json_object"]


def test_real_sdk_sends_schema_then_reuses_it_without_a_saved_option(monkeypatch):
    from openai import OpenAI

    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["response_format"]["type"] == "json_object":
            return httpx.Response(400, json={"error": {"message": FORMAT_ERROR}})
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
                        "message": {"role": "assistant", "content": json.dumps(VALUE)},
                    }
                ],
            },
        )

    with OpenAI(
        api_key="no-key",
        base_url="https://local.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as sdk_client:
        monkeypatch.setattr(OpenAICompatibleBaseClient, "_ensure_client", lambda self: sdk_client)
        client = RoutedLLMClient(config())
        assert client.complete_json([], operation="analysis.style") == VALUE
        assert client.complete_json([], operation="translation.body") == VALUE
    assert modes(bodies) == ["json_object", "json_schema", "json_schema"]
    assert bodies[-1]["response_format"]["json_schema"]["schema"] == {"type": "object"}
