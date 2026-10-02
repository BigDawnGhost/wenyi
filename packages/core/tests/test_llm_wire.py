"""Offline contracts for the profile-driven wire adapters.

These tests never reach a provider: they pin the profile-to-adapter mapping, credential
resolution from the environment, per-wire headers, reasoning placement and the response
checks that keep truncated or empty completions from being treated as answers.
"""

import base64
import json

import httpx
import pytest
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.oauth.credentials import OAuthCredential, OAuthCredentialError
from wenyi_core.llm.profiles import PROFILES, get_profile
from wenyi_core.llm.providers._options import ProviderOptions
from wenyi_core.llm.providers.codex import CodexClient
from wenyi_core.llm.providers.wire.base import WIRE_ADAPTERS, ProfileAdapter
from wenyi_core.llm.providers.wire.gemini_cloudcode import CloudCodeClient, build_request
from wenyi_core.llm.providers.wire.openai_chat import OpenAIChatClient, completion_text
from wenyi_core.llm.providers.wire.responses import ResponsesClient, build_input
from wenyi_core.llm.registry import provider_spec
from wenyi_core.llm.retrying import EmptyResponseError, TruncatedResponseError
from wenyi_core.llm.transport import Messages, RequestContext, ResolvedModel


class StubClient(ProfileAdapter[ProviderOptions]):
    """Concrete adapter that only exercises the shared profile plumbing."""

    def _request(
        self,
        messages: Messages,
        model: ResolvedModel[ProviderOptions],
        *,
        json_mode: bool,
        context: RequestContext,
    ) -> str:
        raise NotImplementedError


def _messages() -> Messages:
    return [{"role": "user", "content": "hello"}]


def test_every_profile_wire_has_an_adapter():
    assert {profile.wire for profile in PROFILES} <= set(WIRE_ADAPTERS)


def test_thinking_is_on_by_default_for_every_profile():
    assert all(profile.default_thinking for profile in PROFILES)


def test_profile_lookup_accepts_aliases():
    assert get_profile("claude") is get_profile("anthropic")
    assert get_profile("grok").kind == "xai"


def test_missing_api_key_names_the_expected_variable(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = StubClient(ProviderConfig(kind="deepseek"))
    with pytest.raises(OAuthCredentialError, match="DEEPSEEK_API_KEY"):
        client.validate_credentials()


def test_api_key_header_is_declared_by_the_profile(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    client = StubClient(ProviderConfig(kind="anthropic"))
    assert client.auth_headers() == {"x-api-key": "secret"}
    assert client.request_headers()["anthropic-version"] == "2023-06-01"


def test_bearer_header_is_the_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    client = StubClient(ProviderConfig(kind="deepseek"))
    assert client.auth_headers() == {"authorization": "Bearer secret"}


def test_endpoint_falls_back_to_the_profile(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    default = StubClient(ProviderConfig(kind="deepseek"))
    override = StubClient(ProviderConfig(kind="deepseek", base_url="https://relay.invalid/v1/"))
    assert default.resolve_base_url() == "https://api.deepseek.com/v1"
    assert override.resolve_base_url() == "https://relay.invalid/v1"


def test_thinking_defaults_come_from_the_profile(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    client = StubClient(ProviderConfig(kind="deepseek"))
    extra_body, top_level = client.reasoning_parts("deepseek-v4-pro", ProviderOptions())
    assert extra_body["thinking"] == {"type": "enabled"}
    assert top_level["reasoning_effort"] == "high"


def test_explicit_thinking_false_disables_reasoning(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    client = StubClient(ProviderConfig(kind="deepseek"))
    extra_body, top_level = client.reasoning_parts(
        "deepseek-v4-pro", ProviderOptions(thinking=False)
    )
    assert extra_body["thinking"] == {"type": "disabled"}
    assert top_level == {}


def test_output_limit_reserves_room_for_thinking():
    thinking = ProviderOptions(thinking=True)
    assert StubClient.output_limit(thinking, 1000, None) == 4096
    assert StubClient.output_limit(thinking, None, 8000) == 8000
    with pytest.raises(ValueError, match="max_output_tokens"):
        StubClient.output_limit(thinking, None, 100)


def test_compatible_endpoint_keeps_the_reasoning_style_escape_hatch(monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "http://localhost:11434/v1")
    client = OpenAIChatClient(
        ProviderConfig(
            kind="openai-compatible",
            base_url="http://localhost:11434/v1",
            reasoning_style="deepseek",
        )
    )
    extra_body, top_level = client.reasoning_placement("llama3", ProviderOptions())
    assert extra_body["thinking"] == {"type": "enabled"}
    assert top_level["reasoning_effort"] == "medium"
    disabled_extra, disabled_top = client.reasoning_placement(
        "llama3", ProviderOptions(thinking=False)
    )
    assert disabled_extra["thinking"] == {"type": "disabled"}
    assert disabled_top == {}


def test_compatible_endpoint_without_style_uses_the_profile(monkeypatch):
    client = OpenAIChatClient(
        ProviderConfig(
            kind="openai-compatible",
            base_url="http://localhost:11434/v1",
        )
    )
    extra_body, top_level = client.reasoning_placement("llama3", ProviderOptions())
    assert extra_body == {}
    assert top_level["reasoning_effort"] == "medium"


def test_unknown_connection_option_is_rejected():
    with pytest.raises(ValueError):
        StubClient(
            ProviderConfig(
                kind="deepseek",
                base_url="https://relay.invalid/v1",
                reasoning_stile="deepseek",
            )
        )


def test_completion_text_reads_the_first_choice():
    payload = {"choices": [{"message": {"content": "translated"}, "finish_reason": "stop"}]}
    assert completion_text(payload, "deepseek") == "translated"


def test_truncated_completion_is_not_an_answer():
    payload = {"choices": [{"message": {"content": "half"}, "finish_reason": "length"}]}
    with pytest.raises(TruncatedResponseError):
        completion_text(payload, "deepseek")


def test_empty_completion_is_rejected():
    with pytest.raises(EmptyResponseError):
        completion_text({"choices": [{"message": {"content": "  "}}]}, "deepseek")
    with pytest.raises(EmptyResponseError):
        completion_text({}, "deepseek")


def test_openai_chat_body_carries_profile_reasoning(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    client = OpenAIChatClient(ProviderConfig(kind="deepseek"))
    bodies: list[dict] = []

    def fake_post(path: str, body: dict, *, headers=None) -> dict:
        bodies.append({"path": path, "body": body, "headers": headers})
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 3}}

    monkeypatch.setattr(client, "post_json", fake_post)
    context = RequestContext(
        operation="translate",
        tier="strong",
        max_tokens=2048,
        emit=lambda *args, **kwargs: None,
        record_usage=lambda sample: None,
        attempt_scope=lambda: None,
    )
    resolved = ResolvedModel(model="deepseek-v4-pro", options=ProviderOptions())
    assert client._request(_messages(), resolved, json_mode=True, context=context) == "ok"
    request = bodies[0]
    assert request["path"] == "/chat/completions"
    assert request["body"]["model"] == "deepseek-v4-pro"
    assert request["body"]["max_tokens"] == 2048
    assert request["body"]["thinking"] == {"type": "enabled"}
    assert request["body"]["reasoning_effort"] == "high"
    assert request["body"]["response_format"] == {"type": "json_object"}
    assert "json" in request["body"]["messages"][-1]["content"].lower()


def _resolved(model: str, *, thinking: bool | None = None) -> ResolvedModel[ProviderOptions]:
    return ResolvedModel(model=model, options=ProviderOptions(thinking=thinking))


def _context(max_tokens: int | None = None) -> RequestContext:
    return RequestContext(
        operation="translation.body",
        tier="strong",
        max_tokens=max_tokens,
        emit=lambda *args, **kwargs: None,
        record_usage=lambda sample: None,
        attempt_scope=lambda: None,
    )


def _json_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )


def _sse_response(*events: dict, content_type: str | None = "text/event-stream") -> httpx.Response:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    headers = {} if content_type is None else {"content-type": content_type}
    return httpx.Response(200, headers=headers, content=body.encode())


def _unread_response(payload: dict) -> httpx.Response:
    """A response whose body has not been read, as ``client.stream`` hands one over."""
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=httpx.ByteStream(json.dumps(payload).encode()),
    )


class _Stream:
    """Stand-in for the streaming POST context manager."""

    def __init__(self, response: httpx.Response):
        self._response = response

    def __enter__(self) -> httpx.Response:
        return self._response

    def __exit__(self, *exc_info) -> bool:
        return False


def _install_response(monkeypatch, client, response: httpx.Response) -> None:
    monkeypatch.setattr(client, "open_stream", lambda *args, **kwargs: _Stream(response))


def test_responses_input_moves_system_prompts_into_instructions():
    items, instructions = build_input(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
    )
    assert instructions == "be terse"
    assert [item["role"] for item in items] == ["user", "assistant"]
    assert items[0]["content"] == [{"type": "input_text", "text": "hello"}]
    assert items[1]["content"] == [{"type": "output_text", "text": "hi"}]


def test_responses_body_carries_profile_fields_for_codex():
    client = CodexClient(ProviderConfig(kind="openai-codex"))
    body = client.build_body(
        _messages(),
        ResolvedModel("gpt-5.4-codex", ProviderOptions()),
        json_mode=False,
        context=_context(),
    )
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["prompt_cache_key"] == client.cache_key
    assert body["stream"] is True
    assert "max_output_tokens" not in body


def test_responses_body_omits_reasoning_when_thinking_is_off():
    client = ResponsesClient(ProviderConfig(kind="xai"))
    body = client.build_body(
        _messages(),
        ResolvedModel("grok-4.6", ProviderOptions(thinking=False)),
        json_mode=False,
        context=_context(),
    )
    assert "reasoning" not in body
    assert "store" not in body


def test_responses_stream_accumulates_text_and_usage(monkeypatch):
    client = ResponsesClient(ProviderConfig(kind="xai"))
    samples: list = []
    _install_response(
        monkeypatch,
        client,
        _sse_response(
            {"type": "response.output_text.delta", "delta": "half"},
            {"type": "response.output_text.done", "text": "half done"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "half done"}],
                        }
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                },
            },
        ),
    )
    context = _context()
    context.record_usage = samples.append
    assert client._request(
        _messages(), _resolved("grok-4.6"), json_mode=False, context=context
    ) == ("half done")
    assert samples[0].prompt_tokens == 3
    assert samples[0].completion_tokens == 2


def test_responses_stream_reports_truncation(monkeypatch):
    client = ResponsesClient(ProviderConfig(kind="xai"))
    _install_response(
        monkeypatch,
        client,
        _sse_response(
            {"type": "response.output_text.delta", "delta": "half"},
            {"type": "response.incomplete", "response": {"status": "incomplete"}},
        ),
    )
    with pytest.raises(TruncatedResponseError):
        client._request(_messages(), _resolved("grok-4.6"), json_mode=False, context=_context())


def test_responses_failure_event_is_not_an_answer(monkeypatch):
    client = ResponsesClient(ProviderConfig(kind="xai"))
    _install_response(
        monkeypatch,
        client,
        _sse_response(
            {
                "type": "response.failed",
                "response": {"status": "failed", "error": {"message": "overloaded"}},
            }
        ),
    )
    with pytest.raises(RuntimeError, match="overloaded"):
        client._request(_messages(), _resolved("grok-4.6"), json_mode=False, context=_context())


def test_responses_reads_a_non_streamed_body(monkeypatch):
    client = ResponsesClient(ProviderConfig(kind="meta-ai"))
    _install_response(
        monkeypatch,
        client,
        _json_response(
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "done"}]}
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
        ),
    )
    assert (
        client._request(
            _messages(), _resolved("muse-spark-1.2"), json_mode=False, context=_context()
        )
        == "done"
    )


def test_responses_reads_a_stream_that_omits_the_content_type(monkeypatch):
    """The Codex endpoint streams without a content-type header."""
    client = ResponsesClient(ProviderConfig(kind="xai"))
    _install_response(
        monkeypatch,
        client,
        _sse_response(
            {"type": "response.output_text.delta", "delta": "done"},
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": "done"}]}
                    ],
                },
            },
            content_type=None,
        ),
    )
    assert (
        client._request(_messages(), _resolved("grok-4.6"), json_mode=False, context=_context())
        == "done"
    )


def test_responses_reads_an_unread_json_body(monkeypatch):
    client = ResponsesClient(ProviderConfig(kind="meta-ai"))
    _install_response(
        monkeypatch,
        client,
        _unread_response(
            {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "done"}]}
                ],
            }
        ),
    )
    assert (
        client._request(
            _messages(), _resolved("muse-spark-1.2"), json_mode=False, context=_context()
        )
        == "done"
    )


def test_responses_refusal_without_text_is_not_an_answer(monkeypatch):
    client = ResponsesClient(ProviderConfig(kind="xai"))
    _install_response(
        monkeypatch,
        client,
        _sse_response(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "cannot help"}],
                        }
                    ],
                },
            }
        ),
    )
    with pytest.raises(EmptyResponseError, match="cannot help"):
        client._request(_messages(), _resolved("grok-4.6"), json_mode=False, context=_context())


def test_codex_endpoint_rejects_an_explicit_output_limit():
    provider = provider_spec("openai-codex")
    assert provider.output_limit(ProviderOptions(), 8192, None) is None
    with pytest.raises(ValueError, match="max_output_tokens"):
        provider.output_limit(ProviderOptions(), None, 4096)


def test_codex_account_header_comes_from_the_access_token():
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}}
    token = "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    client = CodexClient(ProviderConfig(kind="openai-codex"))
    headers = client.credential_headers(OAuthCredential(access_token=token))
    assert headers["ChatGPT-Account-Id"] == "acct_123"
    assert headers["session-id"] == client.cache_key


def test_cloudcode_request_converts_messages_for_gemini():
    request = build_request(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
    )
    assert request["systemInstruction"] == {"role": "system", "parts": [{"text": "be terse"}]}
    assert [content["role"] for content in request["contents"]] == ["user", "model"]


def test_cloudcode_body_wraps_the_request_envelope(monkeypatch):
    monkeypatch.setenv(
        "WENYI_ANTIGRAVITY_OAUTH", '{"access_token": "token", "project_id": "proj-1"}'
    )
    client = CloudCodeClient(ProviderConfig(kind="antigravity"))
    body = client.build_body(
        _messages(),
        ResolvedModel("gemini-3.7-flash-high", ProviderOptions()),
        json_mode=True,
        context=_context(max_tokens=2048),
    )
    assert body["project"] == "proj-1"
    assert body["model"] == "gemini-3.7-flash-high"
    assert body["userAgent"] == "antigravity"
    assert body["requestId"].startswith("agent/")
    assert body["request"]["generationConfig"] == {
        "maxOutputTokens": 2048,
        "responseMimeType": "application/json",
    }


def test_cloudcode_headers_carry_the_client_identity(monkeypatch):
    monkeypatch.setenv("WENYI_ANTIGRAVITY_OAUTH", '{"access_token": "token"}')
    client = CloudCodeClient(ProviderConfig(kind="antigravity"))
    headers = client.request_headers(accept="text/event-stream")
    assert headers["user-agent"].startswith("antigravity/hub/")
    assert headers["authorization"] == "Bearer token"


def test_cloudcode_stream_skips_thinking_parts_and_reads_usage(monkeypatch):
    monkeypatch.setenv("WENYI_ANTIGRAVITY_OAUTH", '{"access_token": "token"}')
    client = CloudCodeClient(ProviderConfig(kind="antigravity"))
    samples: list = []
    _install_response(
        monkeypatch,
        client,
        _sse_response(
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "pondering", "thought": True},
                                    {"text": "译文"},
                                ]
                            }
                        }
                    ]
                }
            },
            {
                "response": {
                    "candidates": [
                        {"content": {"parts": [{"text": "完成"}]}, "finishReason": "STOP"}
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 7,
                        "candidatesTokenCount": 2,
                        "thoughtsTokenCount": 3,
                        "totalTokenCount": 12,
                    },
                }
            },
        ),
    )
    context = _context()
    context.record_usage = samples.append
    text = client._request(
        _messages(), _resolved("gemini-3.7-flash"), json_mode=False, context=context
    )
    assert text == "译文完成"
    assert samples[0].completion_tokens == 5


def test_cloudcode_truncated_and_blocked_replies_are_refused(monkeypatch):
    monkeypatch.setenv("WENYI_ANTIGRAVITY_OAUTH", '{"access_token": "token"}')
    client = CloudCodeClient(ProviderConfig(kind="antigravity"))
    _install_response(
        monkeypatch,
        client,
        _sse_response(
            {
                "response": {
                    "candidates": [
                        {"content": {"parts": [{"text": "半"}]}, "finishReason": "MAX_TOKENS"}
                    ]
                }
            }
        ),
    )
    with pytest.raises(TruncatedResponseError):
        client._request(
            _messages(), _resolved("gemini-3.7-flash"), json_mode=False, context=_context()
        )
    _install_response(
        monkeypatch,
        client,
        _sse_response({"response": {"promptFeedback": {"blockReason": "SAFETY"}}}),
    )
    with pytest.raises(RuntimeError, match="SAFETY"):
        client._request(
            _messages(), _resolved("gemini-3.7-flash"), json_mode=False, context=_context()
        )
