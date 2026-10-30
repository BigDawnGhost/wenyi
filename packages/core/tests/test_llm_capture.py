"""Offline request-scoped telemetry contracts."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier

import pytest
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.providers.fake import FakeClient, FakeProvider
from wenyi_core.llm.retrying import EmptyResponseError
from wenyi_core.llm.router import RoutedLLMClient
from wenyi_core.llm.usage import UsageSample


def test_request_snapshot_is_authoritative_and_independent(monkeypatch):
    config = LLMConfig.model_validate(
        {
            "preset": "fake",
            "models": {"backup": {"provider": "default", "model": "backup"}},
            "routes": {"translation.body": {"tier": "strong", "fallbacks": ["backup"]}},
        }
    )
    client = RoutedLLMClient(config)
    events = []
    client.set_event_sink(lambda event, **data: events.append(event))
    config.models.clear()
    config.providers.clear()
    snapshot = client.request_snapshot("translation.body")
    assert snapshot["primary"]["fingerprint"] == client.routes["translation.body"].fingerprint
    assert snapshot["fallbacks"][0]["model"] == "backup"
    assert snapshot["adapter_protocols"] == {"default": FakeProvider.protocol_version}
    snapshot["primary"]["options"]["changed"] = True
    snapshot["connections"].clear()
    assert client.request_snapshot("translation.body")["connections"]
    assert "changed" not in client.request_snapshot("translation.body")["primary"]["options"]
    assert events == []
    assert FakeClient().request_snapshot("translation.body") is None


def test_nested_capture_restores_parent_without_global_sink():
    client = FakeClient()
    outer, inner = [], []
    with client.capture_events(lambda event, **data: outer.append(event)):
        client._emit_event("before")
        with (
            pytest.raises(ValueError),
            client.capture_events(lambda event, **data: inner.append(event)),
        ):
            client._emit_event("inside")
            raise ValueError("source")
        client._emit_event("after")
    client._emit_event("outside")
    assert outer == ["before", "after"]
    assert inner == ["inside"]


def test_three_concurrent_calls_isolate_scoped_metadata(monkeypatch):
    client = RoutedLLMClient(LLMConfig.model_validate({"preset": "fake"}))
    barrier = Barrier(3)
    sample = UsageSample(7, 3, 10, 4, 3)
    global_events = []
    client.set_event_sink(lambda event, **data: global_events.append((event, data)))

    def request(self, messages, model, *, json_mode, context):
        barrier.wait(timeout=10)
        context.record_usage(sample)
        return messages[0]["content"]

    monkeypatch.setattr(FakeProvider, "_request", request)

    def run(index):
        events = []
        with client.capture_events(lambda event, **data: events.append((event, data))):
            assert client.complete(
                [{"role": "user", "content": str(index)}],
                operation="translation.body",
                max_tokens=100 + index,
            ) == str(index)
        return events

    with ThreadPoolExecutor(max_workers=3) as pool:
        captured = list(pool.map(run, range(3)))
    call_ids = set()
    for index, events in enumerate(captured):
        ids = {data["call_id"] for _, data in events}
        assert len(ids) == 1
        call_ids.update(ids)
        usage = [data for event, data in events if event == "llm_usage"]
        assert len(usage) == 1
        assert usage[0]["attempt"] == 1
        assert all(usage[0][key] == value for key, value in asdict(sample).items())
        for event, data in events:
            assert data["max_output_tokens"] == 100 + index
            assert isinstance(data["adapter_protocol"], int)
            assert not {"options", "messages", "response", "api_key"} & data.keys()
    assert len(call_ids) == 3
    assert len(global_events) == sum(map(len, captured))
    assert client.usage_summary()["totals"]["total_tokens"] == 30


@pytest.mark.parametrize("fail", [False, True])
def test_observer_failure_preserves_result_or_source_error(monkeypatch, fail):
    client = RoutedLLMClient(LLMConfig.model_validate({"preset": "fake"}))
    source = ValueError("source")

    def request(self, messages, model, *, json_mode, context):
        if fail:
            raise source
        context.record_usage(None)
        return "result"

    def observer(event, **data):
        # Rebinding here proves scoped callbacks are outside the global sink lock.
        client.set_event_sink(None)
        raise RuntimeError("observer")

    monkeypatch.setattr(FakeProvider, "_request", request)
    with client.capture_events(observer):
        if fail:
            with pytest.raises(ValueError) as caught:
                client.complete([], operation="translation.body")
            assert caught.value is source
        else:
            assert client.complete([], operation="translation.body") == "result"


def test_usage_matches_retry_and_failover_attempts(monkeypatch):
    config = LLMConfig.model_validate(
        {
            "preset": "fake",
            "providers": {"default": {"kind": "fake", "max_retries": 1}},
            "models": {
                "backup": {"provider": "default", "model": "backup", "max_output_tokens": 50}
            },
            "routes": {"translation.body": {"tier": "strong", "fallbacks": ["backup"]}},
        }
    )
    client = RoutedLLMClient(config)
    monkeypatch.setattr(client.limits, "wait_for_retry", lambda seconds: None)
    events = []
    samples = [UsageSample(1, 2, 3), UsageSample(4, 5, 9)]
    attempts = 0

    def request(self, messages, model, *, json_mode, context):
        nonlocal attempts
        attempts += 1
        context.record_usage(samples[attempts - 1] if attempts <= 2 else None)
        if attempts <= 2:
            raise EmptyResponseError("empty")
        return "backup"

    monkeypatch.setattr(FakeProvider, "_request", request)
    with client.capture_events(lambda event, **data: events.append((event, data))):
        assert client.complete([], operation="translation.body") == "backup"
    usage = [data for event, data in events if event == "llm_usage"]
    started = [data for event, data in events if event == "llm_request_started"]
    assert [data["attempt"] for data in usage] == [1, 2]
    assert [data["attempt"] for data in started] == [1, 2, 3]
    for data, start, sample in zip(usage, started, samples):
        for key in ("call_id", "operation", "model", "provider", "profile", "attempt"):
            assert data[key] == start[key]
        assert all(data[key] == value for key, value in asdict(sample).items())
    assert started[-1]["profile"] == "backup"
    assert len({data["call_id"] for _, data in events}) == 1
    assert client.usage_summary()["totals"]["total_tokens"] == 12
