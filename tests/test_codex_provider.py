"""Offline tests for the experimental Codex ChatGPT provider."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trans_novel.config import Config
from trans_novel.llm.providers.codex import (
    CodexClient,
    CodexOptions,
    extract_codex_usage,
    flatten_messages,
)
from trans_novel.llm.registry import provider_spec
from trans_novel.llm.transport import RequestContext, ResolvedModel


def test_codex_is_registered_separately_from_openai():
    assert "codex" in {
        name for name in __import__("trans_novel.llm.registry", fromlist=["PROVIDERS"]).PROVIDERS
    }
    assert provider_spec("codex").kind == "codex"
    assert provider_spec("openai").kind == "openai"


def test_flatten_messages_keeps_system_and_transcript():
    developer, prompt = flatten_messages(
        [
            {"role": "system", "content": "Be careful."},
            {"role": "user", "content": "Translate this."},
            {"role": "assistant", "content": '{"translations":["甲"]}'},
            {"role": "user", "content": "Polish previous JSON."},
        ]
    )
    assert developer == "Be careful."
    assert "USER:\nTranslate this." in prompt
    assert "ASSISTANT:\n" in prompt
    assert "Polish previous JSON." in prompt


def test_extract_codex_usage_maps_token_breakdown():
    usage = SimpleNamespace(
        total=SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            reasoning_output_tokens=2,
            total_tokens=16,
            cached_input_tokens=3,
        )
    )
    sample = extract_codex_usage(usage)
    assert sample is not None
    assert sample.prompt_tokens == 10
    assert sample.completion_tokens == 6
    assert sample.total_tokens == 16
    assert sample.cache_hit_tokens == 3
    assert sample.cache_miss_tokens == 7


def test_config_accepts_codex_provider_without_api_key_env():
    cfg = Config.from_dict(
        {
            "llm": {
                "providers": {"codex": {"kind": "codex"}},
                "models": {
                    "writer": {
                        "provider": "codex",
                        "model": "gpt-5.6-luna",
                        "options": {"reasoning_effort": "medium"},
                    }
                },
                "tiers": {"strong": "writer", "cheap": "writer", "fast": "writer"},
            }
        }
    )
    assert cfg.llm.providers["codex"].kind == "codex"
    assert cfg.llm.models["writer"].options["reasoning_effort"] == "medium"


def test_codex_request_uses_ephemeral_readonly_thread(monkeypatch):
    recorded = {}

    class FakeThread:
        def run(self, prompt, **kwargs):
            recorded["prompt"] = prompt
            recorded["run_kwargs"] = kwargs
            return SimpleNamespace(
                error=None,
                final_response='{"translations":["甲"]}',
                usage=SimpleNamespace(
                    total=SimpleNamespace(
                        input_tokens=5,
                        output_tokens=2,
                        reasoning_output_tokens=0,
                        total_tokens=7,
                        cached_input_tokens=0,
                    )
                ),
            )

    class FakeCodex:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def thread_start(self, **kwargs):
            recorded["start_kwargs"] = kwargs
            return FakeThread()

        def account(self):
            return SimpleNamespace(
                account=SimpleNamespace(
                    root=SimpleNamespace(email="user@example.com", plan_type="plus", type="chatgpt")
                ),
                requires_openai_auth=True,
            )

        def close(self):
            recorded["closed"] = True

    fake_module = SimpleNamespace(
        Codex=FakeCodex,
        Sandbox=SimpleNamespace(read_only="read-only"),
        ApprovalMode=SimpleNamespace(deny_all="deny_all"),
    )
    fake_reasoning = SimpleNamespace(ReasoningEffort=lambda value: f"effort:{value}")

    usages: list = []
    context = RequestContext(
        operation="translation.body",
        tier="strong",
        max_tokens=None,
        emit=lambda *args, **kwargs: None,
        record_usage=usages.append,
        attempt_scope=lambda: patch("builtins.open"),
    )

    # attempt_scope needs a real context manager
    from contextlib import nullcontext

    context.attempt_scope = nullcontext

    from trans_novel.llm.configuration import ProviderConfig

    with (
        patch("trans_novel.llm.providers.codex._require_openai_codex", return_value=fake_module),
        patch("trans_novel.llm.providers.codex.import_module", return_value=fake_reasoning),
    ):
        client = CodexClient(ProviderConfig(kind="codex"))
        text = client._request(
            [
                {"role": "system", "content": "System"},
                {"role": "user", "content": "Hello"},
            ],
            ResolvedModel(model="gpt-5.6-luna", options=CodexOptions(reasoning_effort="high")),
            json_mode=True,
            context=context,
        )

    assert text == '{"translations":["甲"]}'
    assert recorded["start_kwargs"]["ephemeral"] is True
    assert recorded["start_kwargs"]["sandbox"] == "read-only"
    assert recorded["start_kwargs"]["approval_mode"] == "deny_all"
    assert "Output must be valid json." in recorded["start_kwargs"]["developer_instructions"]
    assert recorded["prompt"].startswith("USER:\nHello")
    assert usages and usages[0].total_tokens == 7


def test_missing_sdk_gives_install_hint():
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openai_codex" or name.startswith("openai_codex."):
            raise ImportError("No module named openai_codex")
        return real_import(name, *args, **kwargs)

    from trans_novel.llm.providers import codex as codex_mod

    with patch("builtins.__import__", side_effect=fake_import):
        with pytest.raises(RuntimeError, match="uv sync --extra codex"):
            codex_mod._require_openai_codex()
