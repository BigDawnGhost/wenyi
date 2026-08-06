"""LLM provider 流式传输测试（不访问网络）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from trans_novel.config import LLMConfig, TierConfig
from trans_novel.llm.providers.deepseek import DeepSeekClient
from trans_novel.llm.providers.gemini import GeminiClient


def _openai_usage(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=prompt,
    )


def _openai_chunk(text: Any = None, usage: Any = None) -> SimpleNamespace:
    choices = []
    if text is not None:
        choices = [SimpleNamespace(delta=SimpleNamespace(content=text))]
    return SimpleNamespace(choices=choices, usage=usage)


def _gemini_usage(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=completion,
        thoughts_token_count=0,
        total_token_count=prompt + completion,
        cached_content_token_count=0,
    )


def _gemini_chunk(text: str, usage: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason="", content=None)],
        usage_metadata=usage,
    )


class _ClosableStream:
    def __init__(self, chunks: list[Any], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __iter__(self):
        yield from self.chunks
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


class _Completions:
    def __init__(self, streams: list[_ClosableStream]) -> None:
        self.streams = list(streams)
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _ClosableStream:
        self.kwargs.append(kwargs)
        return self.streams.pop(0)


def _deepseek_client(*, retries: int = 0) -> DeepSeekClient:
    return DeepSeekClient(
        LLMConfig(
            provider="deepseek",
            base_url="https://example.test",
            stream=True,
            max_retries=retries,
            tiers={"strong": TierConfig(model="deepseek-test")},
        )
    )


def _gemini_client(*, retries: int = 0) -> GeminiClient:
    return GeminiClient(
        LLMConfig(
            provider="gemini",
            stream=True,
            max_retries=retries,
            tiers={"strong": TierConfig(model="gemini-test")},
        )
    )


def test_openai_compatible_stream_assembles_json_and_records_final_usage():
    stream = _ClosableStream(
        [
            _openai_chunk('{"status":'),
            _openai_chunk('"ok"}'),
            _openai_chunk(usage=_openai_usage(12, 5)),
        ]
    )
    completions = _Completions([stream])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = _deepseek_client()

    with patch.object(client, "_ensure_client", return_value=sdk):
        result = client.complete_json(
            [{"role": "user", "content": "return json"}],
            stage="Translator",
        )

    assert result == {"status": "ok"}
    assert stream.closed is True
    assert completions.kwargs[0]["stream"] is True
    assert completions.kwargs[0]["stream_options"] == {"include_usage": True}
    assert client.usage_summary()["totals"]["total_tokens"] == 17
    metric = client.performance_summary().samples[-1]
    assert metric.completion_tokens == 5
    assert metric.prompt_tokens == 12
    assert metric.total_tokens == 17


def test_openai_compatible_stream_retry_discards_partial_text_and_counts_attempts():
    failed = _ClosableStream(
        [_openai_chunk("discard-me", _openai_usage(10, 5))],
        ConnectionError("stream disconnected"),
    )
    succeeded = _ClosableStream(
        [_openai_chunk("complete"), _openai_chunk(usage=_openai_usage(20, 10))]
    )
    completions = _Completions([failed, succeeded])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = _deepseek_client(retries=1)
    events: list[tuple[str, dict[str, Any]]] = []
    client.set_event_sink(lambda event, **data: events.append((event, data)))

    with (
        patch.object(client, "_ensure_client", return_value=sdk),
        patch(
            "trans_novel.llm.retrying.wait_for_provider_retry",
            new=lambda _state: 0,
        ),
    ):
        assert client.complete([]) == "complete"

    assert failed.closed is True
    assert succeeded.closed is True
    assert len(completions.kwargs) == 2
    assert [event for event, _data in events] == ["llm_retry_wait"]
    assert events[0][1]["reason"] == "connection"
    assert events[0][1]["failed_attempt"] == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 45
    metric = client.performance_summary().samples[-1]
    assert metric.completion_tokens == 10
    assert metric.prompt_tokens == 30
    assert metric.total_tokens == 45


def test_gemini_stream_assembles_json_and_records_final_usage():
    stream = _ClosableStream(
        [
            _gemini_chunk('{"status":'),
            _gemini_chunk('"ok"}'),
            SimpleNamespace(
                text=None,
                candidates=[],
                usage_metadata=_gemini_usage(15, 6),
            ),
        ]
    )
    models = SimpleNamespace(
        generate_content_stream=lambda **kwargs: stream,
        generate_content=lambda **kwargs: pytest.fail("used non-streaming Gemini API"),
    )
    client = _gemini_client()
    client._client = SimpleNamespace(models=models)

    result = client.complete_json([{"role": "user", "content": "return json"}])

    assert result == {"status": "ok"}
    assert stream.closed is True
    assert client.usage_summary()["totals"]["total_tokens"] == 21
    metric = client.performance_summary().samples[-1]
    assert metric.completion_tokens == 6
    assert metric.prompt_tokens == 15
    assert metric.total_tokens == 21


def test_gemini_stream_retry_discards_partial_text_and_counts_attempts():
    failed = _ClosableStream(
        [_gemini_chunk("discard-me", _gemini_usage(8, 4))],
        ConnectionError("stream disconnected"),
    )
    succeeded = _ClosableStream(
        [
            _gemini_chunk("complete"),
            SimpleNamespace(
                text=None,
                candidates=[],
                usage_metadata=_gemini_usage(12, 6),
            ),
        ]
    )
    streams = [failed, succeeded]
    models = SimpleNamespace(generate_content_stream=lambda **kwargs: streams.pop(0))
    client = _gemini_client(retries=1)
    client._client = SimpleNamespace(models=models)

    with patch(
        "trans_novel.llm.retrying.wait_for_provider_retry",
        new=lambda _state: 0,
    ):
        assert client.complete([]) == "complete"

    assert failed.closed is True
    assert succeeded.closed is True
    assert client.usage_summary()["totals"]["total_tokens"] == 30
    metric = client.performance_summary().samples[-1]
    assert metric.completion_tokens == 6
    assert metric.prompt_tokens == 20
    assert metric.total_tokens == 30
