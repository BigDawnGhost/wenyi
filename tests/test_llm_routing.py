"""Offline contracts for registered operations and immutable model routing."""

from typing import cast

import pytest

from trans_novel.config import Config
from trans_novel.llm.configuration import LLMConfig
from trans_novel.llm.limits import RequestLimits, RequestStopped
from trans_novel.llm.operations import OperationSpec, register_operations
from trans_novel.llm.providers.fake import FakeProvider
from trans_novel.llm.router import RoutedLLMClient
from trans_novel.llm.routing import inference_snapshot, resolve_routes
from trans_novel.llm.usage import UsageSample


def _graph(**extra):
    return LLMConfig.model_validate(
        {
            "providers": {
                "a": {"kind": "fake", "max_retries": 0, "max_concurrency": 1},
                "b": {"kind": "fake", "base_url": "https://other.invalid", "max_retries": 0},
            },
            "models": {
                "one": {"provider": "a", "model": "first", "max_output_tokens": 128},
                "two": {"provider": "b", "model": "second", "max_output_tokens": 256},
            },
            "tiers": {"strong": "one", "cheap": "two", "fast": "one"},
            **extra,
        }
    )


@pytest.mark.parametrize(
    "override, message",
    [
        ({"tiers": {"strong": "one"}}, "exactly strong"),
        ({"tiers": {"strong": "one", "cheap": "missing", "fast": "one"}}, "unknown model"),
        ({"models": {"one": {"provider": "missing", "model": "x"}}}, "unknown connection"),
        ({"models": {"one": {"provider": "a", "model": ""}}}, "at least 1"),
        ({"routes": {"translation.body": {"tier": "strnog"}}}, "Unknown tier"),
        ({"routes": {"translation.body": {}}}, "exactly one"),
        ({"routes": {"translation.body": {"model": "missing"}}}, "unknown model"),
        ({"routes": {"translation.body": {"model": "one", "fallbacks": ["one"]}}}, "duplicate"),
        ({"routes": {"review.verify": {"model": "one", "fallbacks": ["two"]}}}, "resumable"),
        ({"providers": {"a": {"kind": "fake", "api_key": "forbidden"}}}, "Extra inputs"),
        (
            {
                "providers": {
                    "a": {"kind": "fake", "base_url": "https://user:password@example.invalid"}
                }
            },
            "credentials",
        ),
        (
            {"providers": {"a": {"kind": "fake", "api_key_env": "not an env name"}}},
            "environment variable",
        ),
    ],
)
def test_invalid_graphs_fail_offline(override, message):
    with pytest.raises(ValueError, match=message):
        _graph(**override)


def test_preset_entries_replace_whole_profiles():
    with pytest.raises(ValueError, match="model"):
        LLMConfig.model_validate(
            {
                "preset": "deepseek",
                "models": {
                    "default_strong": {"provider": "default", "options": {"thinking": False}}
                },
            }
        )


@pytest.mark.parametrize(
    "specs, message",
    [
        ([OperationSpec("x.a", "a", "strong"), OperationSpec("x.a", "b", "cheap")], "Duplicate"),
        ([OperationSpec("x.a", "a", inherits="x.b")], "Unknown inherited"),
        (
            [OperationSpec("x.a", "a", inherits="x.b"), OperationSpec("x.b", "b", inherits="x.a")],
            "cycle",
        ),
        ([OperationSpec("x.a", "a", "strong", inherits="x.b")], "exactly one"),
    ],
)
def test_registration_rejects_ambiguous_defaults(specs, message):
    with pytest.raises(ValueError, match=message):
        register_operations(specs)


def test_registration_is_immutable_and_accepts_new_operations():
    specs = register_operations([OperationSpec("context.retrieve", "Retrieve context", "fast")])
    assert specs["context.retrieve"].tier == "fast"
    with pytest.raises(TypeError):
        # Deliberately attempt an unsupported mutation to test runtime immutability.
        cast(dict[str, OperationSpec], specs)["other.operation"] = specs["context.retrieve"]


@pytest.mark.parametrize("field", ["api_key", "extra_headers", "maxTokens", "http_options"])
def test_raw_extensions_cannot_override_transport_or_reveal_secrets(field):
    with pytest.raises(ValueError) as caught:
        LLMConfig.model_validate(
            {
                "preset": "deepseek",
                "models": {
                    "default_strong": {
                        "provider": "default",
                        "model": "m",
                        "options": {"extra_body": {field: "secret-value-must-not-appear"}},
                    }
                },
            }
        )
    assert "secret-value-must-not-appear" not in str(caught.value)


def test_invalid_credentials_field_is_redacted_and_empty_list_is_not_a_preset():
    with pytest.raises(ValueError) as caught:
        LLMConfig.model_validate(
            {
                "preset": "fake",
                "providers": {
                    "default": {"kind": "fake", "api_key": "secret-value-must-not-appear"}
                },
            }
        )
    assert "secret-value-must-not-appear" not in str(caught.value)
    with pytest.raises(ValueError):
        Config.from_dict({"llm": []})


def test_native_and_compatible_adapters_can_run_together():
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace as NS

    config = LLMConfig.model_validate(
        {
            "preset": "deepseek",
            "providers": {"native": {"kind": "gemini"}},
            "models": {
                "editor": {
                    "provider": "native",
                    "model": "native-editor",
                    "max_output_tokens": 2000,
                }
            },
            "routes": {"polish.body": {"model": "editor"}},
        }
    )
    client = RoutedLLMClient(config)
    requests = {}

    def compatible(**kwargs):
        requests["compatible"] = kwargs
        return NS(
            choices=[NS(message=NS(content="translation"))],
            usage=NS(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        )

    def native(**kwargs):
        requests["native"] = kwargs
        return NS(
            text="polished",
            candidates=[NS(finish_reason="STOP")],
            usage_metadata=NS(
                prompt_token_count=15, candidates_token_count=5, total_token_count=20
            ),
        )

    client.adapter("default")._client = NS(chat=NS(completions=NS(create=compatible)))
    client.adapter("native")._client = NS(models=NS(generate_content=native))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(
            client.complete, [{"role": "user", "content": "source"}], operation="translation.body"
        )
        second = pool.submit(
            client.complete, [{"role": "user", "content": "draft"}], operation="polish.body"
        )
        assert (first.result(), second.result()) == ("translation", "polished")
    assert requests["compatible"]["model"] == "deepseek-flash"
    assert requests["native"]["model"] == "native-editor"
    assert requests["native"]["config"]["max_output_tokens"] == 2000
    assert "reasoning_effort" not in requests["native"]["config"]
    assert client.usage_summary()["totals"]["total_tokens"] == 30
    assert len(client.usage_summary()["by_provider"]) == 2


def test_all_model_call_sites_use_operations():
    import ast
    from pathlib import Path

    from trans_novel.llm.operations import OPERATIONS

    root = Path(__file__).parents[1] / "trans_novel"
    literal_operations = set()
    for directory in ("agents", "pipeline", "glossary", "srt"):
        for path in (root / directory).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"complete", "complete_json", "_ask_text", "_ask_json"}:
                    continue
                keywords = {item.arg: item.value for item in node.keywords}
                assert "operation" in keywords, path
                assert not {"tier", "stage"}.intersection(keywords), path
                if isinstance(keywords["operation"], ast.Constant):
                    operation = keywords["operation"].value
                    assert operation in OPERATIONS, path
                    literal_operations.add(operation)
    assert {
        "translation.body",
        "translation.title",
        "synopsis.chapter",
        "synopsis.book",
        "glossary.extract",
        "glossary.align_history",
        "srt.translate",
    } <= literal_operations


def test_concurrent_connections_share_usage_without_mixing_requests(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    client = RoutedLLMClient(_graph())
    barrier = Barrier(2)
    seen = []

    def request(adapter, messages, model, *, json_mode, context):
        barrier.wait(timeout=2)
        seen.append((adapter.cfg.base_url, model.model, context.operation, context.max_tokens))
        context.record_usage(UsageSample(prompt_tokens=7, completion_tokens=3, total_tokens=10))
        messages[0]["content"] = "transport mutation"
        return model.model

    monkeypatch.setattr(FakeProvider, "_request", request)
    messages = [{"role": "user", "content": "original"}]
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.complete, messages, operation="translation.body")
        second = pool.submit(client.complete, messages, operation="review.scan")
        assert (first.result(), second.result()) == ("first", "second")
    assert messages[0]["content"] == "original"
    assert sorted(seen, key=lambda row: row[1]) == [
        (None, "first", "translation.body", 128),
        ("https://other.invalid", "second", "review.scan", 256),
    ]
    summary = client.usage_summary()
    assert summary["totals"]["calls"] == 2
    for group in ("by_stage", "by_provider", "by_model", "by_tier"):
        assert sum(row["total_tokens"] for row in summary[group].values()) == 20
    assert client.adapter("a") is client.adapter("a")


def test_router_freezes_original_config_and_direct_selection(monkeypatch):
    config = _graph(routes={"translation.body": {"model": "two"}})
    client = RoutedLLMClient(config)
    config.models.clear()
    monkeypatch.setattr(FakeProvider, "_request", lambda self, messages, model, **kw: model.model)
    assert client.complete([], operation="translation.body") == "second"
    assert client.routes["translation.body"].tier is None
    with pytest.raises(ValueError, match="Unknown model operation"):
        client.complete([], operation="translation.typo")


def test_dynamic_output_hint_respects_explicit_profile_cap(monkeypatch):
    client = RoutedLLMClient(_graph())
    observed = []
    client.set_event_sink(
        lambda event, **data: observed.append(data) if event == "llm_request_started" else None
    )
    requested = []

    def request(self, messages, model, *, json_mode, context):
        requested.append(context.max_tokens)
        return "ok"

    monkeypatch.setattr(FakeProvider, "_request", request)
    client.complete([], operation="annotation.align", max_tokens=999)
    assert client.routes["annotation.align"].max_output_tokens == 256
    assert requested == [256]
    config = Config.from_dict({"llm": {"preset": "deepseek"}})
    assert resolve_routes(config.llm)["synopsis.chapter"].max_output_tokens == 4096
    with pytest.raises(ValueError, match="Thinking mode"):
        Config.from_dict(
            {
                "llm": {
                    "preset": "deepseek",
                    "models": {
                        "default_fast": {
                            "provider": "default",
                            "model": "x",
                            "max_output_tokens": 600,
                        }
                    },
                }
            }
        )


def test_retry_releases_permit_and_records_every_returned_usage(monkeypatch):
    from trans_novel.llm.retrying import EmptyResponseError

    config = _graph()
    raw = config.model_dump()
    raw["providers"]["a"]["max_retries"] = 1
    client = RoutedLLMClient(LLMConfig.model_validate(raw))
    events = []
    client.set_event_sink(lambda event, **data: events.append({"event": event, **data}))
    calls = []

    def request(self, messages, model, *, json_mode, context):
        calls.append(model.model)
        context.record_usage(UsageSample(prompt_tokens=2, completion_tokens=1, total_tokens=3))
        if len(calls) == 1:
            raise EmptyResponseError("empty")
        return "ok"

    def wait(delay):
        assert client.limits._active["a"] == 0

    monkeypatch.setattr(FakeProvider, "_request", request)
    monkeypatch.setattr(client.limits, "wait_for_retry", wait)
    assert client.complete([], operation="translation.body") == "ok"
    assert client.usage_summary()["totals"]["total_tokens"] == 6
    starts = [row for row in events if row["event"] == "llm_request_started"]
    assert [row["attempt"] for row in starts] == [1, 2]
    assert len({row["call_id"] for row in events}) == 1
    assert all(row["model"] == "first" and row["operation"] == "translation.body" for row in events)


def test_only_explicit_stateless_failover_is_used(monkeypatch):
    config = _graph(routes={"translation.body": {"model": "one", "fallbacks": ["two"]}})
    client = RoutedLLMClient(config)
    calls = []

    def request(self, messages, model, **kw):
        calls.append(model.model)
        if model.model == "first":
            raise TimeoutError
        return "fallback"

    monkeypatch.setattr(FakeProvider, "_request", request)
    assert client.complete([], operation="translation.body") == "fallback"
    assert calls == ["first", "second"]
    calls.clear()
    with pytest.raises(TimeoutError):
        client.complete([], operation="polish.body")
    assert calls == ["first"]


class _ProviderDeniedError(Exception):
    status_code = 401


@pytest.mark.parametrize(
    "error", [_ProviderDeniedError, ValueError], ids=["http-401", "local-value-error"]
)
def test_non_retryable_errors_neither_retry_nor_fail_over(monkeypatch, error):
    config = _graph(routes={"translation.body": {"model": "one", "fallbacks": ["two"]}})
    client = RoutedLLMClient(config)
    calls = []

    def request(self, messages, model, **kw):
        calls.append(model.model)
        raise error("denied")

    monkeypatch.setattr(FakeProvider, "_request", request)
    with pytest.raises(error):
        client.complete([], operation="translation.body")
    assert calls == ["first"]


def test_token_preflight_checks_fallback_caps():
    raw = _graph(
        routes={"translation.body": {"model": "one", "fallbacks": ["two"]}},
        budget={"max_tokens": 10000},
    ).model_dump()
    raw["models"]["two"]["max_output_tokens"] = None
    client = RoutedLLMClient(LLMConfig.model_validate(raw))
    with pytest.raises(ValueError, match="max_output_tokens"):
        client.validate_credentials(("translation.body",))


def test_shared_quota_across_aliases_and_usage_releases_reservations():
    raw = _graph(
        quotas={"account": {"requests_per_minute": 1}}, budget={"max_tokens": 1000}
    ).model_dump()
    for connection in raw["providers"].values():
        connection["quota_group"] = "account"
    now = [0.0]
    limits = RequestLimits(LLMConfig.model_validate(raw), clock=lambda: now[0])
    with limits.attempt("a", 900, lambda *a, **k: None) as reservation:
        reservation.actual_tokens = 10
    reasons = []

    def advance(event, **data):
        reasons.append(data["reason"])
        now[0] = 61

    with limits.attempt("b", 900, advance):
        pass
    assert reasons == ["requests_per_minute"]
    assert limits.reserved_tokens == 910


def test_operations_share_connection_concurrency(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client = RoutedLLMClient(_graph())
    started, waiting, release = Event(), Event(), Event()
    calls = []
    client.set_event_sink(
        lambda event, **data: waiting.set() if event == "llm_request_waiting" else None
    )

    def request(self, messages, model, **kwargs):
        calls.append(model.model)
        started.set()
        assert release.wait(2)
        return "ok"

    monkeypatch.setattr(FakeProvider, "_request", request)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.complete, [], operation="translation.body")
        try:
            assert started.wait(2)
            second = pool.submit(client.complete, [], operation="polish.body")
            assert waiting.wait(2)
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=2) == second.result(timeout=2) == "ok"
    assert len(calls) == 2


def test_tpm_waits_for_the_shared_account_window():
    raw = _graph(quotas={"account": {"tokens_per_minute": 100}}).model_dump()
    for connection in raw["providers"].values():
        connection["quota_group"] = "account"
    now = [0.0]
    limits = RequestLimits(LLMConfig.model_validate(raw), clock=lambda: now[0])
    with limits.attempt("a", 80, lambda *args, **kwargs: None):
        pass
    reasons = []

    def advance(event, **data):
        reasons.append(data["reason"])
        now[0] = 61

    with limits.attempt("b", 30, advance):
        pass
    assert reasons == ["tokens_per_minute"]


def test_request_budget_deadline_and_cancel_stop_before_fallback(monkeypatch):
    client = RoutedLLMClient(_graph(budget={"max_requests": 1}))
    client.complete([], operation="translation.body")
    with pytest.raises(RequestStopped, match="budget"):
        client.complete([], operation="translation.body")
    now = [0.0]
    limits = RequestLimits(_graph(budget={"deadline_seconds": 1}), clock=lambda: now[0])
    now[0] = 2
    with pytest.raises(RequestStopped, match="deadline"):
        limits.check()
    other = RequestLimits(_graph())
    other.cancel()
    with pytest.raises(RequestStopped, match="cancelled"):
        other.wait_for_retry(600)


def test_inference_fingerprint_ignores_aliases_credentials_and_transport_controls():
    before = _graph()
    raw = before.model_dump()
    raw["providers"]["renamed"] = {
        **raw["providers"].pop("a"),
        "api_key_env": "ANOTHER_KEY",
        "timeout": 12,
        "max_retries": 99,
        "max_concurrency": 20,
    }
    raw["models"]["renamed"] = {**raw["models"].pop("one"), "provider": "renamed"}
    raw["tiers"]["strong"] = raw["tiers"]["fast"] = "renamed"
    after = LLMConfig.model_validate(raw)
    assert inference_snapshot(before, ("translation.body",)) == inference_snapshot(
        after, ("translation.body",)
    )
    raw["models"]["renamed"]["model"] = "different"
    changed = LLMConfig.model_validate(raw)
    assert inference_snapshot(before, ("translation.body",)) != inference_snapshot(
        changed, ("translation.body",)
    )


def test_preset_and_operation_override_are_independent():
    from trans_novel.llm.routing import resolve_routes

    cfg = Config.from_dict(
        {
            "llm": {
                "preset": "deepseek",
                "providers": {"local": {"kind": "fake"}},
                "models": {"editor": {"provider": "local", "model": "editor-v1"}},
                "routes": {"review.verify": {"model": "editor"}},
            }
        }
    )
    routes = resolve_routes(cfg.llm)
    assert routes["review.verify"].model == "editor-v1"
    assert routes["review.scan"].provider_kind == "deepseek"
    assert routes["autofix.verify"].model == "editor-v1"
    assert routes["review.fix"].provider_kind == "deepseek"


def test_unknown_operation_fails_before_constructing_clients():
    with pytest.raises(ValueError, match="review.verfy"):
        Config.from_dict(
            {"llm": {"preset": "deepseek", "routes": {"review.verfy": {"tier": "strong"}}}}
        )


def test_route_rejects_ambiguous_selection():
    with pytest.raises(ValueError, match="exactly one"):
        Config.from_dict(
            {
                "llm": {
                    "preset": "deepseek",
                    "routes": {"review.verify": {"tier": "strong", "model": "default_strong"}},
                }
            }
        )
