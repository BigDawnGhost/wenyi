"""LLM 用量统计契约测试（离线，不发网络请求）。"""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from wenyi_core.agents.base import Agent
from wenyi_core.config import Config, LLMConfig, TierConfig
from wenyi_core.llm.factory import build_client
from wenyi_core.llm.providers._openai_compatible import normalize_openai_usage
from wenyi_core.llm.providers.deepseek import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DeepSeekClient,
)
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.providers.openai import OpenAIClient
from wenyi_core.llm.usage import (
    UsageSample,
    UsageTracker,
    make_usage_sample,
    merge_usage_summaries,
    usage_delta,
)
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.runstore import RunStore
from wenyi_core.storage import FileStorage
from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt


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


def _make_response(content: str, usage: Any) -> Any:
    msg = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=msg)
    return SimpleNamespace(choices=[choice], usage=usage)


class _CompletionsStub:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self._idx = 0

    def create(self, **kwargs: Any) -> Any:
        if self._idx >= len(self._responses):
            raise AssertionError("stub 响应已耗尽")
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


def _minimal_deepseek_cfg() -> LLMConfig:
    return LLMConfig(
        provider="deepseek",
        base_url="x",
        api_key_env="X",
        timeout=1,
        max_retries=0,
        tiers={
            "strong": TierConfig(model="m1"),
            "cheap": TierConfig(model="m2"),
        },
    )


class TestDeepSeekProviderDefaults(unittest.TestCase):
    def test_provider_only_config_uses_deepseek_defaults(self):
        client = build_client(Config.from_dict({"llm": {"provider": "deepseek"}}))
        self.assertIsInstance(client, DeepSeekClient)
        assert isinstance(client, DeepSeekClient)

        self.assertEqual(client.base_url, DEFAULT_BASE_URL)
        self.assertEqual(client.api_key_env, DEFAULT_API_KEY_ENV)
        self.assertEqual(client.tiers["strong"].model, "deepseek-v4-pro")
        self.assertEqual(client.tiers["cheap"].model, "deepseek-v4-flash")
        self.assertTrue(client.tiers["strong"].options.thinking)
        self.assertFalse(client.tiers["fast"].options.thinking)

    def test_explicit_config_overrides_provider_defaults(self):
        client = DeepSeekClient(_minimal_deepseek_cfg())

        self.assertEqual(client.base_url, "x")
        self.assertEqual(client.api_key_env, "X")
        self.assertEqual(client.tiers["strong"].model, "m1")

    def test_partial_tier_override_keeps_other_provider_defaults(self):
        client = DeepSeekClient(
            LLMConfig(
                tiers={
                    "fast": TierConfig(
                        model="custom-fast",
                        options={"thinking": False},
                    ),
                }
            )
        )

        self.assertEqual(client.tiers["fast"].model, "custom-fast")
        self.assertEqual(client.tiers["strong"].model, "deepseek-v4-pro")
        self.assertEqual(client.tiers["cheap"].model, "deepseek-v4-flash")

    def test_provider_option_can_be_overridden_without_repeating_model(self):
        client = DeepSeekClient(
            LLMConfig(
                tiers={
                    "fast": TierConfig(options={"thinking": True}),
                }
            )
        )

        self.assertEqual(client.tiers["fast"].model, "deepseek-v4-flash")
        self.assertTrue(client.tiers["fast"].options.thinking)

    def test_unknown_provider_option_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_option"):
            DeepSeekClient(
                LLMConfig(
                    tiers={
                        "strong": TierConfig(options={"unknown_option": True}),
                    }
                )
            )


class TestDeepSeekUsageByTier(unittest.TestCase):
    def test_records_usage_and_splits_by_tier(self):
        cfg = _minimal_deepseek_cfg()
        c = DeepSeekClient(cfg)
        responses = [
            _make_response(
                "strong-out",
                _make_usage(
                    prompt_tokens=1000,
                    completion_tokens=200,
                    total_tokens=1200,
                    prompt_cache_hit_tokens=800,
                    prompt_cache_miss_tokens=200,
                ),
            ),
            _make_response(
                "cheap-out",
                _make_usage(
                    prompt_tokens=500,
                    completion_tokens=100,
                    total_tokens=600,
                    prompt_cache_hit_tokens=100,
                    prompt_cache_miss_tokens=400,
                ),
            ),
        ]
        msgs = [{"role": "user", "content": "hi"}]
        with patch.object(c, "_ensure_client", return_value=_ClientStub(responses)):
            self.assertEqual(
                c.complete(msgs, tier="strong", stage="Translator"), "strong-out"
            )
            self.assertEqual(c.complete(msgs, tier="cheap"), "cheap-out")

        summary = c.usage_summary()
        totals = summary["totals"]
        self.assertEqual(totals["prompt_tokens"], 1500)
        self.assertEqual(totals["completion_tokens"], 300)
        self.assertEqual(totals["total_tokens"], 1800)
        self.assertEqual(totals["cache_hit_tokens"], 900)
        self.assertEqual(totals["cache_miss_tokens"], 600)
        self.assertEqual(totals["cache_hit_rate"], 0.6)
        self.assertEqual(totals["calls"], 2)

        by_tier = summary["by_tier"]
        self.assertEqual(by_tier["strong"]["cache_hit_rate"], 0.8)
        self.assertEqual(by_tier["cheap"]["cache_hit_rate"], 0.2)
        self.assertEqual(by_tier["strong"]["calls"], 1)
        self.assertEqual(by_tier["cheap"]["calls"], 1)
        self.assertEqual(by_tier["strong"]["prompt_tokens"], 1000)
        self.assertEqual(by_tier["cheap"]["prompt_tokens"], 500)
        self.assertEqual(list(summary["by_stage"]), ["Translator"])
        self.assertEqual(summary["by_stage"]["Translator"]["total_tokens"], 1200)
        self.assertEqual(summary["by_stage"]["Translator"]["cache_hit_rate"], 0.8)


class TestOpenAIUsageNormalization(unittest.TestCase):
    def test_nested_cached_tokens_are_normalized(self):
        cfg = LLMConfig(
            provider="openai",
            base_url="x",
            api_key_env="X",
            timeout=1,
            max_retries=0,
            tiers={"strong": TierConfig(model="m")},
        )
        client = OpenAIClient(cfg)
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        )
        response = _make_response("ok", usage)

        with patch.object(
            client,
            "_ensure_client",
            return_value=_ClientStub([response]),
        ):
            self.assertEqual(
                client.complete(
                    [{"role": "user", "content": "x"}],
                    stage="Translator",
                ),
                "ok",
            )

        summary = client.usage_summary()
        self.assertEqual(summary["totals"]["cache_hit_tokens"], 40)
        self.assertEqual(summary["totals"]["cache_miss_tokens"], 60)
        self.assertEqual(summary["totals"]["cache_hit_rate"], 0.4)
        self.assertEqual(
            summary["by_stage"]["Translator"]["cache_hit_rate"],
            0.4,
        )

    def test_missing_cache_details_remain_unknown(self):
        sample = normalize_openai_usage(
            SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
            )
        )
        tracker = UsageTracker()
        tracker.record("strong", sample)
        totals = tracker.summary()["totals"]
        self.assertEqual(totals["cache_hit_tokens"], 0)
        self.assertEqual(totals["cache_miss_tokens"], 0)


class TestMissingUsage(unittest.TestCase):
    def test_none_usage_silently_skipped(self):
        tracker = UsageTracker()
        tracker.record("strong", None)
        summary = tracker.summary()
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["totals"]["total_tokens"], 0)
        self.assertEqual(summary["by_tier"], {})
        self.assertEqual(summary["by_stage"], {})

    def test_complete_with_none_usage_does_not_count(self):
        cfg = _minimal_deepseek_cfg()
        c = DeepSeekClient(cfg)
        with patch.object(
            c,
            "_ensure_client",
            return_value=_ClientStub([_make_response("ok", None)]),
        ):
            self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "ok")
        summary = c.usage_summary()
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["totals"]["total_tokens"], 0)
        self.assertEqual(summary["by_tier"], {})
        self.assertEqual(summary["by_stage"], {})

    def test_complete_with_missing_usage_attr_does_not_count(self):
        cfg = _minimal_deepseek_cfg()
        c = DeepSeekClient(cfg)
        msg = SimpleNamespace(content="ok")
        choice = SimpleNamespace(message=msg)
        # 无 usage 属性
        resp = SimpleNamespace(choices=[choice])
        with patch.object(c, "_ensure_client", return_value=_ClientStub([resp])):
            self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "ok")
        summary = c.usage_summary()
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["by_tier"], {})

    def test_missing_total_tokens_falls_back_to_prompt_plus_completion(self):
        tracker = UsageTracker()
        usage = _make_usage(prompt_tokens=40, completion_tokens=10)
        # 确认未设置 total_tokens
        self.assertFalse(hasattr(usage, "total_tokens"))
        tracker.record("cheap", make_usage_sample(usage))
        slot = tracker.summary()["by_tier"]["cheap"]
        self.assertEqual(slot["prompt_tokens"], 40)
        self.assertEqual(slot["completion_tokens"], 10)
        self.assertEqual(slot["total_tokens"], 50)
        self.assertEqual(slot["calls"], 1)


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
        self.assertEqual(totals["prompt_tokens"], 0)
        self.assertEqual(totals["completion_tokens"], 0)
        self.assertEqual(totals["cache_hit_tokens"], 0)
        self.assertEqual(totals["cache_miss_tokens"], 0)
        self.assertEqual(c.usage_summary()["by_tier"], {})
        self.assertEqual(c.usage_summary()["by_stage"], {})


class TestUsageThreadSafety(unittest.TestCase):
    def test_concurrent_record_exact_counts(self):
        client = FakeClient()
        n_workers = 8
        per_worker = 25  # 8 * 25 = 200
        usage = UsageSample(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cache_hit_tokens=3,
            cache_miss_tokens=7,
        )

        def _worker() -> None:
            for _ in range(per_worker):
                client.usage.record("strong", usage)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = [pool.submit(_worker) for _ in range(n_workers)]
            for f in concurrent.futures.as_completed(futs):
                f.result()

        total_calls = n_workers * per_worker
        summary = client.usage_summary()
        totals = summary["totals"]
        self.assertEqual(totals["calls"], total_calls)
        self.assertEqual(totals["prompt_tokens"], 10 * total_calls)
        self.assertEqual(totals["completion_tokens"], 5 * total_calls)
        self.assertEqual(totals["total_tokens"], 15 * total_calls)
        self.assertEqual(totals["cache_hit_tokens"], 3 * total_calls)
        self.assertEqual(totals["cache_miss_tokens"], 7 * total_calls)
        self.assertEqual(totals["cache_hit_rate"], 0.3)  # 3/(3+7)
        self.assertEqual(summary["by_tier"]["strong"]["calls"], total_calls)


class TestAgentStageAttribution(unittest.TestCase):
    def test_agent_helpers_pass_class_name_as_stage(self):
        client = FakeClient()
        agent = Agent(client, Config.from_dict({"llm": {"provider": "fake"}}))

        agent._ask_text("system", "user", tier="strong")
        agent._ask_json("system", "user", tier="cheap", default=[])

        self.assertEqual([call["stage"] for call in client.calls], ["Agent", "Agent"])


class TestUsageIncrementalPersistence(unittest.TestCase):
    @staticmethod
    def _record(
        client: FakeClient,
        tier: str,
        *,
        prompt: int,
        completion: int,
        stage: str | None = None,
    ) -> None:
        client.usage.record(
            tier,
            UsageSample(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                cache_hit_tokens=prompt // 2,
                cache_miss_tokens=prompt - prompt // 2,
            ),
            stage,
        )

    def test_delta_and_merge_do_not_double_count(self):
        client = FakeClient()
        self._record(
            client, "strong", prompt=100, completion=20, stage="Translator"
        )
        first = client.usage_summary()
        self._record(
            client, "strong", prompt=50, completion=10, stage="Translator"
        )
        self._record(client, "fast", prompt=30, completion=5, stage="Synopsizer")
        second = client.usage_summary()

        increment = usage_delta(second, first)
        self.assertEqual(increment["totals"]["total_tokens"], 95)
        self.assertEqual(increment["by_stage"]["Translator"]["total_tokens"], 60)
        self.assertEqual(increment["by_stage"]["Synopsizer"]["total_tokens"], 35)
        merged = merge_usage_summaries(first, increment)
        self.assertEqual(merged, second)

    def test_usage_accumulates_across_orchestrators_for_one_book(self):
        with tempfile.TemporaryDirectory() as d:
            store = FileStorage(os.path.join(d, "state", "book"))
            config = Config.from_dict({"llm": {"provider": "fake"}})

            first_client = FakeClient()
            first = Orchestrator(config, client=first_client)
            first.storage = store
            self._record(
                first_client,
                "strong",
                prompt=100,
                completion=20,
                stage="Translator",
            )
            cumulative = first._flush_usage(scope="translate")
            self.assertEqual(cumulative["totals"]["total_tokens"], 120)

            # 同一进程再次 flush 没有新增调用，不能重复累计。
            unchanged = first._flush_usage(scope="pipeline")
            self.assertEqual(unchanged["totals"]["total_tokens"], 120)

            # 模拟 resume：新 client / Orchestrator 的增量继续累加到同一本书。
            resumed_client = FakeClient()
            resumed = Orchestrator(config, client=resumed_client)
            resumed.storage = store
            self._record(
                resumed_client,
                "cheap",
                prompt=40,
                completion=10,
                stage="Reviewer",
            )
            cumulative = resumed._flush_usage(scope="translate")

            self.assertEqual(cumulative["totals"]["total_tokens"], 170)
            self.assertEqual(cumulative["totals"]["calls"], 2)
            self.assertEqual(cumulative["by_tier"]["strong"]["total_tokens"], 120)
            self.assertEqual(cumulative["by_tier"]["cheap"]["total_tokens"], 50)
            self.assertEqual(cumulative["by_stage"]["Translator"]["total_tokens"], 120)
            self.assertEqual(cumulative["by_stage"]["Reviewer"]["total_tokens"], 50)
            self.assertEqual(store.load_usage(), cumulative)
            self.assertTrue(os.path.isfile(store.usage_path))

    def test_report_omits_usage_and_usage_file_keeps_book_total(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "novel.txt")
            write_sample_txt(source)
            config = Config.from_dict(
                {
                    "language": {"source": "ja", "target": "zh"},
                    "llm": {"provider": "fake"},
                    "pipeline": {"book_understanding": False, "review": False},
                    "paths": {"state_dir": os.path.join(d, "state")},
                }
            )

            initial_client = FakeClient(handler=routing_handler)
            initial = Orchestrator(config, client=initial_client)
            store = initial.run_steps(source, {"translate"})["storage"]
            self._record(initial_client, "strong", prompt=100, completion=20)
            initial._flush_usage(scope="translate")

            resumed_client = FakeClient(handler=routing_handler)
            resumed = Orchestrator(config, client=resumed_client)
            self._record(resumed_client, "cheap", prompt=40, completion=10)
            result = resumed.run_steps(source, {"report"})

            self.assertNotIn("usage", result["report"])
            usage = result["storage"].load_usage()
            self.assertIsNotNone(usage)
            assert usage is not None
            self.assertEqual(usage["totals"]["total_tokens"], 170)
            self.assertEqual(usage["totals"]["calls"], 2)
            self.assertEqual(result["storage"].load_usage(), usage)


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
