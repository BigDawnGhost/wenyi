"""LLM 用量统计契约测试（schema v2，离线，不发网络请求）。

覆盖：totals 仅累计一次；by_agent / by_operation / by_provider / by_model /
by_stage 分别归因（同一物理/逻辑事件在 by_agent 与 by_operation 各计一次）；
实际请求的尝试、失败和空响应均记账；delta/merge 不重不漏、拒绝非 v2 快照；
RunStore 断点续跑时正确累计。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tenacity import wait_none

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.config import Config, LLMConfig, ModelRef
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.providers.transport import OpenAICompatibleTransport
from trans_novel.llm.retrying import EmptyResponseError
from trans_novel.llm.usage import UsageTracker, merge_usage_summaries, usage_delta
from trans_novel.pipeline import Application
from trans_novel.pipeline.state import RunStore


def _make_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    prompt_cache_hit_tokens: int = 0,
    prompt_cache_miss_tokens: int = 0,
) -> Any:
    """构造普通 class 实例作为 usage（非 dict）。"""
    u = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_cache_hit_tokens=prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=prompt_cache_miss_tokens,
    )
    if total_tokens is not None:
        u.total_tokens = total_tokens
    return u


def _make_response(content: Any, usage: Any) -> Any:
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice], usage=usage)


class _CompletionsStub:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self._idx = 0
        self.calls: list[dict[str, Any]] = []  # 记录请求 kwargs，供契约断言

    def create(self, **kwargs: Any) -> Any:
        if self._idx >= len(self._responses):
            raise AssertionError("stub 响应已耗尽")
        self.calls.append(kwargs)
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


class _ChatStub:
    def __init__(self, responses: list[Any]) -> None:
        self.completions = _CompletionsStub(responses)


class _ClientStub:
    """支持 stub.chat.completions.create(**kwargs) 的最小客户端。"""

    def __init__(self, responses: list[Any]) -> None:
        self.chat = _ChatStub(responses)


def _transport(tracker: UsageTracker):
    config = LLMConfig.model_validate(
        {
            "models": {
                "translator": ["deepseek/m1"],
                "analyst": ["deepseek/m1"],
                "editor": ["deepseek/m1"],
                "fast": ["deepseek/m2"],
            },
        }
    )
    return OpenAICompatibleTransport(
        config,
        tracker,
        provider="deepseek",
        provider_name="DeepSeek",
        default_base_url="https://api.deepseek.com",
        default_api_key_env="DEEPSEEK_API_KEY",
        requires_api_key=True,
    )


class TestUsageDimensions(unittest.TestCase):
    def test_no_sink_calls_provider_without_telemetry_setup(self):
        tracker = UsageTracker()
        transport = _transport(tracker)
        client = _ClientStub([_make_response("ok", None)])
        transport._client = client

        with (
            patch(
                "trans_novel.llm.providers.transport.uuid.uuid4",
                side_effect=AssertionError("no-sink calls must not create telemetry ids"),
            ),
            patch(
                "trans_novel.llm.providers.transport._request_hash",
                side_effect=AssertionError("no-sink calls must not hash requests"),
            ),
        ):
            result = transport.complete(
                [{"role": "user", "content": "x"}],
                ModelRef("deepseek", "m1"),
                agent="translator",
                operation="translate.batch",
            )

        self.assertEqual(result, "ok")
        self.assertEqual(
            client.chat.completions.calls,
            [
                {
                    "model": "m1",
                    "messages": [{"role": "user", "content": "x"}],
                    "stream": False,
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            ],
        )

    def test_records_tokens_once_into_totals_and_parallel_views(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        responses = [
            _make_response(
                "a",
                _make_usage(
                    prompt_tokens=1000,
                    completion_tokens=200,
                    total_tokens=1200,
                    prompt_cache_hit_tokens=800,
                    prompt_cache_miss_tokens=200,
                ),
            ),
            _make_response(
                "b",
                _make_usage(
                    prompt_tokens=500,
                    completion_tokens=100,
                    total_tokens=600,
                    prompt_cache_hit_tokens=100,
                    prompt_cache_miss_tokens=400,
                ),
            ),
        ]
        t._client = _ClientStub(responses)
        ref = ModelRef("deepseek", "m1")
        msgs = [{"role": "user", "content": "hi"}]
        self.assertEqual(
            t.complete(
                msgs, ref, stage="Translator", agent="translator", operation="translate.batch"
            ),
            "a",
        )
        self.assertEqual(
            t.complete(msgs, ref, agent="translator", operation="translate.batch"), "b"
        )

        summary = tracker.summary()
        totals = summary["totals"]
        self.assertEqual(totals["prompt_tokens"], 1500)
        self.assertEqual(totals["completion_tokens"], 300)
        self.assertEqual(totals["total_tokens"], 1800)
        self.assertEqual(totals["cache_hit_tokens"], 900)
        self.assertEqual(totals["cache_miss_tokens"], 600)
        self.assertEqual(totals["cache_hit_rate"], 0.6)
        self.assertEqual(totals["calls"], 2)

        agent = summary["by_operation"]["translate.batch"]
        self.assertEqual(agent["calls"], 2)
        self.assertEqual(agent["prompt_tokens"], 1500)
        self.assertEqual(agent["cache_hit_rate"], 0.6)
        self.assertEqual(agent["attempts"], 2)
        self.assertEqual(agent["failed_attempts"], 0)
        # 同一事件在 by_agent（功能 Agent 视图）里同样累计
        by_agent = summary["by_agent"]["translator"]
        self.assertEqual(by_agent["calls"], 2)
        self.assertEqual(by_agent["prompt_tokens"], 1500)
        self.assertEqual(by_agent["total_tokens"], 1800)

        provider = summary["by_provider"]["deepseek"]
        self.assertEqual(provider["calls"], 2)
        self.assertEqual(provider["total_tokens"], 1800)
        self.assertNotIn("logical_calls", provider)
        self.assertNotIn("accepted", provider)

        model = summary["by_model"]["deepseek:m1"]
        self.assertEqual(model["calls"], 2)
        self.assertEqual(model["total_tokens"], 1800)
        self.assertEqual(model["attempts"], 2)

        # stage 归因：显式标注的调用进 by_stage；未标注的只进其它维度
        by_stage = summary["by_stage"]
        self.assertEqual(list(by_stage), ["Translator"])
        self.assertEqual(by_stage["Translator"]["calls"], 1)
        self.assertEqual(by_stage["Translator"]["prompt_tokens"], 1000)
        self.assertEqual(summary["schema_version"], 2)

    def test_missing_usage_records_attempt_but_no_token_call(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        t._client = _ClientStub([_make_response("ok", None)])
        t.complete(
            [{"role": "user", "content": "x"}],
            ModelRef("deepseek", "m1"),
            agent="translator",
            operation="translate.batch",
        )
        summary = tracker.summary()
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["totals"]["total_tokens"], 0)
        agent = summary["by_operation"]["translate.batch"]
        self.assertEqual(agent["attempts"], 1)
        self.assertEqual(agent["calls"], 0)
        self.assertEqual(summary["by_agent"]["translator"]["attempts"], 1)

    def test_missing_total_tokens_falls_back_to_prompt_plus_completion(self):
        tracker = UsageTracker()
        usage = _make_usage(prompt_tokens=40, completion_tokens=10)
        self.assertFalse(hasattr(usage, "total_tokens"))
        tracker.record(
            provider="deepseek",
            model_ref=ModelRef("deepseek", "m1"),
            agent="translator",
            operation="translate.batch",
            usage=usage,
        )
        slot = tracker.summary()["by_operation"]["translate.batch"]
        self.assertEqual(slot["total_tokens"], 50)
        self.assertEqual(slot["calls"], 1)

    def test_reasoning_tokens_direct_and_nested(self):
        tracker = UsageTracker()
        direct = _make_usage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        direct.reasoning_tokens = 7
        nested = _make_usage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        nested.completion_tokens_details = SimpleNamespace(reasoning_tokens=4)
        for usage in (direct, nested):
            tracker.record(
                provider="deepseek",
                model_ref=ModelRef("deepseek", "m1"),
                agent="editor",
                operation="polish.batch",
                usage=usage,
            )
        slot = tracker.summary()["by_operation"]["polish.batch"]
        self.assertEqual(slot["reasoning_tokens"], 11)
        self.assertEqual(slot["total_tokens"], 60)  # reasoning 不叠加进 total
        self.assertNotIn("reasoning_tokens", tracker.summary()["totals"])
        self.assertEqual(tracker.summary()["by_agent"]["editor"]["reasoning_tokens"], 11)

    def test_totals_direct_once_not_derived_from_dimensions(self):
        tracker = UsageTracker()
        tracker.record(
            provider="p1",
            model_ref=ModelRef("p1", "m1"),
            agent="a1",
            operation="op1",
            usage=_make_usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        tracker.record(
            provider="p1",
            model_ref=ModelRef("p1", "m2"),
            agent="a2",
            operation="op2",
            usage=_make_usage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
        )
        summary = tracker.summary()
        self.assertEqual(summary["totals"]["calls"], 2)
        self.assertEqual(summary["totals"]["total_tokens"], 45)
        self.assertEqual(summary["by_provider"]["p1"]["calls"], 2)
        self.assertEqual(summary["by_provider"]["p1"]["total_tokens"], 45)
        self.assertEqual(summary["by_operation"]["op1"]["total_tokens"], 15)
        self.assertEqual(summary["by_operation"]["op2"]["total_tokens"], 30)


class TestThinkingFlagWiring(unittest.TestCase):
    def test_thinking_explicit_enable_or_disable(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        t._client = _ClientStub([_make_response("a", None), _make_response("b", None)])
        msgs = [{"role": "user", "content": "x"}]
        t.complete(
            msgs,
            ModelRef("deepseek", "m1", reasoning_enabled=True),
            agent="translator",
            operation="translate.batch",
        )
        t.complete(
            msgs,
            ModelRef("deepseek", "m1", reasoning_enabled=False),
            agent="translator",
            operation="translate.batch",
        )
        on, off = t._client.chat.completions.calls
        self.assertEqual(on["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(on["reasoning_effort"], "high")
        self.assertEqual(off["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", off)

    def test_same_service_id_accepts_per_request_reasoning_policy(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        t._client = _ClientStub([_make_response("a", None), _make_response("b", None)])
        msgs = [{"role": "user", "content": "x"}]
        t.complete(
            msgs,
            ModelRef("deepseek", "deepseek-v4-flash", reasoning_enabled=True),
            agent="editor",
            operation="polish.batch",
        )
        t.complete(
            msgs,
            ModelRef("deepseek", "deepseek-v4-flash", reasoning_enabled=False),
            agent="preparer",
            operation="terms.mine",
        )
        think, fast = t._client.chat.completions.calls
        self.assertEqual(think["model"], "deepseek-v4-flash")
        self.assertEqual(think["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(think["reasoning_effort"], "high")
        self.assertEqual(fast["model"], "deepseek-v4-flash")
        self.assertEqual(fast["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", fast)


class TestEmptyResponseAccounting(unittest.TestCase):
    def test_empty_then_success_preserves_usage_and_attribution(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        responses = [
            _make_response("", _make_usage(prompt_tokens=3, completion_tokens=1, total_tokens=4)),
            _make_response(
                "  preserved output  ",
                _make_usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            ),
        ]
        t._client = _ClientStub(responses)
        with patch(
            "trans_novel.llm.providers.transport.wait_exponential", return_value=wait_none()
        ):
            result = t.complete(
                [{"role": "user", "content": "x"}],
                ModelRef("deepseek", "m1"),
                stage="Translator",
                agent="translator",
                operation="translate.batch",
            )
        self.assertEqual(result, "  preserved output  ")
        self.assertEqual(len(t._client.chat.completions.calls), 2)
        summary = tracker.summary()
        self.assertEqual(summary["by_stage"]["Translator"]["calls"], 2)
        self.assertEqual(summary["by_stage"]["Translator"]["total_tokens"], 11)
        agent = summary["by_operation"]["translate.batch"]
        self.assertEqual(agent["calls"], 2)
        self.assertEqual(agent["total_tokens"], 11)
        self.assertEqual(agent["attempts"], 2)
        self.assertEqual(agent["failed_attempts"], 1)
        self.assertEqual(summary["by_model"]["deepseek:m1"]["failed_attempts"], 1)

    def test_whitespace_empty_retries(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        t._client = _ClientStub([_make_response(" \n\t", None), _make_response("visible", None)])
        with patch(
            "trans_novel.llm.providers.transport.wait_exponential", return_value=wait_none()
        ):
            self.assertEqual(
                t.complete(
                    [{"role": "user", "content": "x"}],
                    ModelRef("deepseek", "m1"),
                    agent="translator",
                    operation="translate.batch",
                ),
                "visible",
            )
        self.assertEqual(len(t._client.chat.completions.calls), 2)

    def test_missing_choices_retries_as_empty_response(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        malformed = SimpleNamespace(choices=None, usage=None)
        t._client = _ClientStub([malformed, _make_response("visible", None)])
        with patch(
            "trans_novel.llm.providers.transport.wait_exponential", return_value=wait_none()
        ):
            self.assertEqual(
                t.complete(
                    [{"role": "user", "content": "x"}],
                    ModelRef("deepseek", "m1"),
                    agent="preparer",
                    operation="terms.mine",
                ),
                "visible",
            )
        self.assertEqual(len(t._client.chat.completions.calls), 2)
        self.assertEqual(tracker.summary()["by_operation"]["terms.mine"]["failed_attempts"], 1)

    def test_exhaustion_raises_and_keeps_consumed_usage(self):
        tracker = UsageTracker()
        t = _transport(tracker)
        t._client = _ClientStub(
            [
                _make_response(
                    "", _make_usage(prompt_tokens=2, completion_tokens=1, total_tokens=3)
                ),
                _make_response(
                    "", _make_usage(prompt_tokens=4, completion_tokens=2, total_tokens=6)
                ),
            ]
        )
        with (
            patch("trans_novel.llm.providers.transport.wait_exponential", return_value=wait_none()),
            patch("trans_novel.llm.providers.transport._MAX_RETRIES", 1),
            self.assertRaises(EmptyResponseError),
        ):
            t.complete(
                [{"role": "user", "content": "x"}],
                ModelRef("deepseek", "m1"),
                stage="Translator",
                agent="translator",
                operation="translate.batch",
            )
        summary = tracker.summary()
        self.assertEqual(summary["by_stage"]["Translator"]["calls"], 2)
        self.assertEqual(summary["by_stage"]["Translator"]["total_tokens"], 9)
        agent = summary["by_operation"]["translate.batch"]
        self.assertEqual(agent["calls"], 2)
        self.assertEqual(agent["total_tokens"], 9)
        self.assertEqual(agent["attempts"], 2)
        self.assertEqual(agent["failed_attempts"], 2)

    def test_permanent_error_does_not_retry(self):
        """普通 RuntimeError 是永久错误：传输不重试、立即原样抛出。"""
        tracker = UsageTracker()
        t = _transport(tracker)

        class _Boom:
            def create(self, **kwargs):
                raise RuntimeError("down")

        t._client = SimpleNamespace(chat=SimpleNamespace(completions=_Boom()))
        with self.assertRaisesRegex(RuntimeError, "down"):
            t.complete(
                [{"role": "user", "content": "x"}],
                ModelRef("deepseek", "m1"),
                agent="translator",
                operation="translate.batch",
            )
        agent = tracker.summary()["by_operation"]["translate.batch"]
        self.assertEqual(agent["attempts"], 1, "永久错误只尝试一次")
        self.assertEqual(agent["failed_attempts"], 1)


class TestUsageDeltaAndMerge(unittest.TestCase):
    @staticmethod
    def _record(
        tracker: UsageTracker, *, agent: str, operation: str, prompt: int, completion: int
    ) -> None:
        tracker.record(
            provider="deepseek",
            model_ref=ModelRef("deepseek", "m1"),
            agent=agent,
            operation=operation,
            usage=_make_usage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                prompt_cache_hit_tokens=prompt // 2,
                prompt_cache_miss_tokens=prompt - prompt // 2,
            ),
        )

    def test_delta_and_merge_do_not_double_count(self):
        tracker = UsageTracker()
        self._record(
            tracker, agent="translator", operation="translate.batch", prompt=100, completion=20
        )
        first = tracker.summary()
        self._record(
            tracker, agent="translator", operation="translate.batch", prompt=50, completion=10
        )
        self._record(tracker, agent="editor", operation="polish.batch", prompt=30, completion=5)
        second = tracker.summary()

        increment = usage_delta(second, first)
        self.assertEqual(increment["totals"]["total_tokens"], 95)
        self.assertEqual(
            increment["by_operation"]["translate.batch"]["prompt_tokens"],
            50,
            "已持久化的部分不进增量",
        )
        merged = merge_usage_summaries(first, increment)
        self.assertEqual(merged, second)
        self.assertEqual(
            set(merged),
            {
                "schema_version",
                "totals",
                "by_agent",
                "by_operation",
                "by_provider",
                "by_model",
                "by_stage",
            },
        )

    def test_merge_combines_both_agent_and_operation_views(self):
        tracker = UsageTracker()
        self._record(
            tracker, agent="translator", operation="translate.batch", prompt=100, completion=20
        )
        first = tracker.summary()
        tracker.record_outcome("translator", "translate.batch", accepted=True)
        merged = merge_usage_summaries(first, usage_delta(tracker.summary(), first))
        self.assertEqual(merged["by_agent"]["translator"]["accepted"], 1)
        self.assertEqual(merged["by_operation"]["translate.batch"]["accepted"], 1)
        self.assertEqual(merged["by_agent"]["translator"]["total_tokens"], 120)
        self.assertEqual(merged["by_operation"]["translate.batch"]["total_tokens"], 120)

    def test_empty_accumulated_allowed_as_fresh_snapshot(self):
        tracker = UsageTracker()
        self._record(
            tracker, agent="translator", operation="translate.batch", prompt=10, completion=5
        )
        merged = merge_usage_summaries({}, tracker.summary())
        self.assertEqual(merged["totals"]["total_tokens"], 15)
        self.assertEqual(merged["by_agent"]["translator"]["total_tokens"], 15)
        self.assertEqual(merged["by_operation"]["translate.batch"]["total_tokens"], 15)

    def test_merge_rejects_unsupported_schema(self):
        legacy = {
            "totals": {"calls": 1, "total_tokens": 100},
            "by_tier": {"strong": {"calls": 1, "total_tokens": 100}},
            "by_operation": {"translate.batch": {"calls": 1, "total_tokens": 100}},
        }
        with self.assertRaisesRegex(ValueError, "不支持的 usage 快照 schema"):
            merge_usage_summaries(legacy, UsageTracker().summary())
        with self.assertRaisesRegex(ValueError, "不支持的 usage 快照 schema"):
            merge_usage_summaries({"schema_version": 1, "totals": {}}, UsageTracker().summary())

    def test_merge_rejects_unsupported_increment_schema(self):
        with self.assertRaisesRegex(ValueError, "不支持的 usage 快照 schema"):
            merge_usage_summaries(UsageTracker().summary(), {"totals": {"calls": 1}})

    def test_new_snapshots_never_write_legacy_fields(self):
        tracker = UsageTracker()
        self._record(
            tracker, agent="translator", operation="translate.batch", prompt=10, completion=5
        )
        summary = tracker.summary()
        self.assertNotIn("legacy_by_tier", summary)
        self.assertNotIn("by_tier", summary)
        delta = usage_delta(summary, UsageTracker().summary())
        self.assertNotIn("legacy_by_tier", delta)


class TestUsageIncrementalPersistence(unittest.TestCase):
    def test_usage_accumulates_across_applications_for_one_book(self):
        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "state", "book"))
            config = Config.from_dict({"llm": fake_llm_dict()})

            first_client = FakeClient()
            first = Application(config, client=first_client)
            first.bind_usage(store, scope="translate")
            first_client.usage.record(
                provider="fake",
                model_ref=ModelRef("fake", "p"),
                agent="translator",
                operation="translate.batch",
                usage=_make_usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            )
            self.assertEqual(store.load_usage()["totals"]["total_tokens"], 120)
            first.finish_usage("translate")

            resumed_client = FakeClient()
            resumed = Application(config, client=resumed_client)
            resumed.bind_usage(store, scope="translate")
            resumed_client.usage.record(
                provider="fake",
                model_ref=ModelRef("fake", "p"),
                agent="editor",
                operation="polish.batch",
                usage=_make_usage(prompt_tokens=40, completion_tokens=10, total_tokens=50),
            )
            cumulative = store.load_usage()
            resumed.finish_usage("translate")

            self.assertEqual(cumulative["totals"]["total_tokens"], 170)
            self.assertEqual(cumulative["totals"]["calls"], 2)
            self.assertEqual(cumulative["by_agent"]["translator"]["total_tokens"], 120)
            self.assertEqual(cumulative["by_agent"]["editor"]["total_tokens"], 50)
            self.assertEqual(cumulative["by_operation"]["translate.batch"]["total_tokens"], 120)
            self.assertEqual(cumulative["by_operation"]["polish.batch"]["total_tokens"], 50)
            self.assertEqual(cumulative["by_provider"]["fake"]["total_tokens"], 170)
            self.assertTrue(os.path.isfile(store.usage_path))
            with open(store.event_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            usage_events = [e for e in events if e["event"] == "usage_summary"]
            self.assertTrue(usage_events)
            self.assertEqual(usage_events[-1].get("event_schema"), 2)
            self.assertEqual(usage_events[-1]["scope"], "translate")
            self.assertIn("increment", usage_events[-1])
            self.assertNotIn("cumulative", usage_events[-1], "累计用量不进事件，只落 usage.json")
            self.assertFalse(any(e["event"] == "usage_snapshot" for e in events))

    def test_operation_only_failure_persists_without_stage_flush(self):
        """失败调用的 attempts/failed_attempts/logical_calls 在响应无 token 时也即时落盘。"""
        from trans_novel.agents.base import Agent

        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "state", "book"))
            config = Config.from_dict({"llm": fake_llm_dict()})

            def _boom(messages, agent, operation, json_mode):
                raise RuntimeError("model down")

            client = FakeClient(handler=_boom)
            orch = Application(config, client=client)
            orch.bind_usage(store, scope="translate")
            agent = Agent(client, config)

            result = agent._ask_json(
                "sys", "user", default={}, agent="translator", operation="translate.batch"
            )
            self.assertEqual(result, {}, "default 应吞掉异常，Agent 调用方视角照常返回")
            before = client.usage_summary()["by_operation"]["translate.batch"]
            persisted = store.load_usage()
            orch.finish_usage("translate")

            self.assertEqual(
                persisted["by_operation"]["translate.batch"]["attempts"], before["attempts"]
            )
            self.assertEqual(
                persisted["by_operation"]["translate.batch"]["failed_attempts"],
                before["failed_attempts"],
            )
            self.assertEqual(
                persisted["by_operation"]["translate.batch"]["logical_calls"],
                before["logical_calls"],
            )
            self.assertEqual(
                persisted["by_agent"]["translator"]["logical_calls"], before["logical_calls"]
            )


class TestOperationTelemetry(unittest.TestCase):
    def test_new_agent_slot_has_full_canonical_field_set(self):
        c = FakeClient()
        c.complete(
            [{"role": "user", "content": "x"}], agent="translator", operation="translate.batch"
        )
        summary = c.usage_summary()
        for slot in (summary["by_agent"]["translator"], summary["by_operation"]["translate.batch"]):
            for key in (
                "calls",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cache_hit_tokens",
                "cache_miss_tokens",
                "cache_hit_rate",
                "logical_calls",
                "attempts",
                "failed_attempts",
                "elapsed_ms",
                "reasoning_tokens",
                "accepted",
                "rejected",
                "fallbacks",
            ):
                self.assertIn(key, slot)
            # FakeClient 不产生真实 provider usage：token/cache 字段保持 0，
            # 但 agent/operation 标签本身与 attempts/logical_calls 必须被记录。
            self.assertEqual(slot["calls"], 0)
            self.assertEqual(slot["prompt_tokens"], 0)
            self.assertEqual(slot["attempts"], 1)
            self.assertEqual(slot["logical_calls"], 1)
            self.assertEqual(slot["failed_attempts"], 0)
            self.assertEqual(slot["fallbacks"], 0)

    def test_handler_exception_records_failed_attempt_then_reraises(self):
        def _boom(messages, agent, operation, json_mode):
            raise ValueError("bad")

        c = FakeClient(handler=_boom)
        with self.assertRaises(ValueError):
            c.complete([{"role": "user", "content": "x"}], agent="editor", operation="polish.batch")
        op = c.usage_summary()["by_operation"]["polish.batch"]
        self.assertEqual(op["attempts"], 1)
        self.assertEqual(op["failed_attempts"], 1)
        self.assertEqual(op["logical_calls"], 1)
        self.assertEqual(c.usage_summary()["by_agent"]["editor"]["failed_attempts"], 1)

    def test_outcome_and_fallback_counters(self):
        c = FakeClient()
        c.complete(
            [{"role": "user", "content": "x"}], agent="translator", operation="translate.batch"
        )
        c.usage.record_outcome("translator", "translate.batch", accepted=True)
        c.usage.record_outcome("translator", "translate.batch", accepted=False)
        c.usage.record_fallback("translator", "translate.batch")
        for slot in (
            c.usage_summary()["by_agent"]["translator"],
            c.usage_summary()["by_operation"]["translate.batch"],
        ):
            self.assertEqual(slot["accepted"], 1)
            self.assertEqual(slot["rejected"], 1)
            self.assertEqual(slot["fallbacks"], 1)

    def test_concurrent_calls_do_not_lose_or_corrupt_records(self):
        """FakeClient.calls 只用一把锁保护列表本身，never 持锁调用 handler；
        并发下 calls 长度与各 agent/operation 的 logical_calls 计数必须精确，不丢不重。"""
        c = FakeClient(handler=lambda m, a, o, j: "ok")
        n = 64

        def _one(i):
            c.complete(
                [{"role": "user", "content": str(i)}], agent="editor", operation="polish.batch"
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_one, range(n)))

        self.assertEqual(len(c.calls), n)
        op = c.usage_summary()["by_operation"]["polish.batch"]
        self.assertEqual(op["logical_calls"], n)
        self.assertEqual(op["attempts"], n)
        self.assertEqual(c.usage_summary()["by_agent"]["editor"]["logical_calls"], n)


class TestUsageThreadSafety(unittest.TestCase):
    def test_concurrent_record_exact_counts(self):
        tracker = UsageTracker()
        n_workers = 8
        per_worker = 25  # 8 * 25 = 200
        usage = _make_usage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_cache_hit_tokens=3,
            prompt_cache_miss_tokens=7,
        )
        ref = ModelRef("deepseek", "m1")

        def _worker() -> None:
            for _ in range(per_worker):
                tracker.record(
                    provider="deepseek",
                    model_ref=ref,
                    agent="translator",
                    operation="translate.batch",
                    usage=usage,
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = [pool.submit(_worker) for _ in range(n_workers)]
            for f in concurrent.futures.as_completed(futs):
                f.result()

        total_calls = n_workers * per_worker
        summary = tracker.summary()
        totals = summary["totals"]
        self.assertEqual(totals["calls"], total_calls)
        self.assertEqual(totals["prompt_tokens"], 10 * total_calls)
        self.assertEqual(totals["completion_tokens"], 5 * total_calls)
        self.assertEqual(totals["total_tokens"], 15 * total_calls)
        self.assertEqual(totals["cache_hit_tokens"], 3 * total_calls)
        self.assertEqual(totals["cache_miss_tokens"], 7 * total_calls)
        self.assertEqual(totals["cache_hit_rate"], 0.3)  # 3/(3+7)
        self.assertEqual(summary["by_agent"]["translator"]["calls"], total_calls)
        self.assertEqual(summary["by_operation"]["translate.batch"]["calls"], total_calls)
        self.assertEqual(summary["by_provider"]["deepseek"]["calls"], total_calls)
        self.assertEqual(summary["by_model"]["deepseek:m1"]["calls"], total_calls)


class TestEmptyCacheHitRate(unittest.TestCase):
    def test_fresh_client_zero_hit_rate_and_full_keys(self):
        c = FakeClient()
        totals = c.usage_summary()["totals"]
        self.assertEqual(totals["cache_hit_rate"], 0.0)
        self.assertEqual(totals["total_tokens"], 0)
        for key in (
            "calls",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "cache_hit_rate",
        ):
            self.assertIn(key, totals)
        self.assertEqual(totals["calls"], 0)
        summary = c.usage_summary()
        self.assertEqual(summary["by_agent"], {})
        self.assertEqual(summary["by_operation"], {})
        self.assertEqual(summary["by_provider"], {})
        self.assertEqual(summary["by_model"], {})
        self.assertEqual(summary["by_stage"], {})
        self.assertEqual(summary["schema_version"], 2)


class TestRunStoreLock(unittest.TestCase):
    def test_second_store_waits_for_first_store_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "state", "book")
            first = RunStore(run_dir)
            second = RunStore(run_dir)
            entered = threading.Event()

            def acquire_second() -> None:
                with second.lock():
                    entered.set()

            with first.lock():
                worker = threading.Thread(target=acquire_second)
                worker.start()
                self.assertFalse(entered.wait(0.1))

            self.assertTrue(entered.wait(1))
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
