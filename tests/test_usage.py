"""Offline LLM usage contracts without network requests."""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.fake_llm import routing_handler
from tests.model_fixtures import model_config
from tests.sample_data import write_sample_txt
from trans_novel.agents.base import Agent
from trans_novel.config import Config, LLMConfig
from trans_novel.ingest.models import Chapter, Document, Segment
from trans_novel.llm.factory import build_client
from trans_novel.llm.providers._openai_compatible import (
    EmptyResponseError,
    normalize_openai_usage,
)
from trans_novel.llm.providers.deepseek import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DeepSeekClient,
)
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.router import RoutedLLMClient
from trans_novel.llm.usage import (
    UsageSample,
    UsageTracker,
    make_usage_sample,
    merge_usage_summaries,
    usage_delta,
)
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.runstore import RunStore, source_sha256


def _make_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    prompt_cache_hit_tokens: int = 0,
    prompt_cache_miss_tokens: int = 0,
) -> Any:
    """Build usage as a regular class instance instead of a dictionary."""
    u = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_cache_hit_tokens=prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=prompt_cache_miss_tokens,
    )
    if total_tokens is not None:
        u.total_tokens = total_tokens
    return u


def _make_response(
    content: str,
    usage: Any,
    *,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
) -> Any:
    msg = SimpleNamespace(content=content, reasoning_content=reasoning_content)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
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
    """Minimal client supporting stub.chat.completions.create(**kwargs)."""

    def __init__(self, responses: list[Any]) -> None:
        self.chat = _ChatStub(responses)


class _MeteredFakeClient(FakeClient):
    """Record fixed tokens per offline call for per-run ledger assertions."""

    def complete(
        self,
        messages,
        *,
        operation: str,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> str:
        result = super().complete(
            messages,
            operation=operation,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
        self.usage.record(
            self.routes[operation].tier or "direct",
            UsageSample(
                prompt_tokens=7,
                completion_tokens=3,
                total_tokens=10,
                cache_hit_tokens=2,
                cache_miss_tokens=5,
            ),
            operation,
        )
        return result


def _minimal_deepseek_cfg() -> LLMConfig:
    return model_config(
        kind="deepseek",
        base_url="https://example.invalid/v1",
        api_key_env="X",
        timeout=1,
        max_retries=0,
        profiles={
            "strong": dict(model="m1"),
            "cheap": dict(model="m2"),
        },
    )


def _minimal_openai_compatible_cfg(
    *,
    max_retries: int = 0,
    reasoning_fallback: bool = False,
) -> LLMConfig:
    options = {"json_response_fallback": "reasoning_content"} if reasoning_fallback else {}
    return model_config(
        kind="openai-compatible",
        base_url="https://example.invalid/v1",
        max_retries=max_retries,
        profiles={"strong": dict(model="m", options=options)},
    )


class TestOpenAICompatibleReasoningContent(unittest.TestCase):
    def test_json_mode_falls_back_to_reasoning_content_when_content_is_empty(self):
        reasoning_content = '{"translations":["译文"]}'
        client = RoutedLLMClient(_minimal_openai_compatible_cfg(reasoning_fallback=True))
        response = _make_response(
            "",
            None,
            reasoning_content=reasoning_content,
        )

        with patch.object(
            client.adapter("default"), "_ensure_client", return_value=_ClientStub([response])
        ):
            self.assertEqual(
                client.complete(
                    [{"role": "user", "content": "translate"}],
                    json_mode=True,
                    operation="translation.body",
                ),
                reasoning_content,
            )

    def test_complete_json_rejects_mixed_reasoning_content(self):
        reasoning_content = (
            '示例：{"translations":["翻译后的中文"]}\n最终输出：{"translations":["越过山口……"]}'
        )
        client = RoutedLLMClient(_minimal_openai_compatible_cfg(reasoning_fallback=True))
        response = _make_response(
            "",
            None,
            reasoning_content=reasoning_content,
        )

        with patch.object(
            client.adapter("default"), "_ensure_client", return_value=_ClientStub([response])
        ):
            with self.assertRaisesRegex(EmptyResponseError, "fallback response is invalid JSON"):
                client.complete_json(
                    [{"role": "user", "content": "translate"}], operation="translation.body"
                )

    def test_complete_json_rejects_reasoning_content_after_length_finish(self):
        client = RoutedLLMClient(_minimal_openai_compatible_cfg(reasoning_fallback=True))
        response = _make_response(
            "",
            None,
            reasoning_content='{"translations":["翻译后的中文"]}',
            finish_reason="length",
        )

        with patch.object(
            client.adapter("default"), "_ensure_client", return_value=_ClientStub([response])
        ):
            with self.assertRaises(RuntimeError):
                client.complete_json(
                    [{"role": "user", "content": "translate"}], operation="translation.body"
                )

    def test_plain_mode_retries_empty_content_instead_of_using_reasoning(self):
        client = RoutedLLMClient(_minimal_openai_compatible_cfg(reasoning_fallback=True))
        response = _make_response(
            "",
            None,
            reasoning_content="不要把这段思考当成译文",
        )

        with patch.object(
            client.adapter("default"), "_ensure_client", return_value=_ClientStub([response])
        ):
            with self.assertRaisesRegex(EmptyResponseError, "content is empty"):
                client.complete(
                    [{"role": "user", "content": "translate"}], operation="translation.body"
                )

    def test_enabled_json_fallback_retries_when_reasoning_content_is_blank(self):
        client = RoutedLLMClient(_minimal_openai_compatible_cfg(reasoning_fallback=True))
        response = _make_response("", None, reasoning_content=" \n ")

        with patch.object(
            client.adapter("default"), "_ensure_client", return_value=_ClientStub([response])
        ):
            with self.assertRaisesRegex(EmptyResponseError, "content is empty"):
                client.complete_json(
                    [{"role": "user", "content": "translate"}], operation="translation.body"
                )

    def test_default_json_mode_retries_empty_content_without_reading_reasoning(self):
        client = RoutedLLMClient(_minimal_openai_compatible_cfg(max_retries=1))
        responses = [
            _make_response(
                " \n ",
                None,
                reasoning_content='{"translations":["错误的推理内容"]}',
            ),
            _make_response('{"translations":["正确响应"]}', None),
        ]
        stub = _ClientStub(responses)
        events: list[dict[str, Any]] = []
        client.set_event_sink(
            lambda event, **data: (
                events.append({"event": event, **data}) if event.startswith("llm_retry_") else None
            )
        )

        with patch.object(client.adapter("default"), "_ensure_client", return_value=stub):
            self.assertEqual(
                client.complete(
                    [{"role": "user", "content": "translate"}],
                    json_mode=True,
                    operation="translation.body",
                ),
                '{"translations":["正确响应"]}',
            )
        self.assertEqual(stub.chat.completions._idx, 2)
        self.assertEqual([event["event"] for event in events], ["llm_retry_wait"])
        self.assertEqual(events[0]["reason"], "empty_response")

    def test_json_mode_prefers_content_when_both_fields_exist(self):
        content = '{"translations":["content"]}'
        client = RoutedLLMClient(_minimal_openai_compatible_cfg(reasoning_fallback=True))
        response = _make_response(
            content,
            None,
            reasoning_content="这是一段明显不是 JSON 的推理文本",
        )

        with patch.object(
            client.adapter("default"), "_ensure_client", return_value=_ClientStub([response])
        ):
            self.assertEqual(
                client.complete(
                    [{"role": "user", "content": "translate"}],
                    json_mode=True,
                    operation="translation.body",
                ),
                content,
            )


class TestDeepSeekProviderDefaults(unittest.TestCase):
    def test_provider_only_config_uses_deepseek_defaults(self):
        client = build_client(Config.from_dict({"llm": {"preset": "deepseek"}}))
        self.assertIsInstance(client.adapter("default"), DeepSeekClient)
        assert isinstance(client.adapter("default"), DeepSeekClient)

        self.assertEqual(client.adapter("default").base_url, DEFAULT_BASE_URL)
        self.assertEqual(client.adapter("default").api_key_env, DEFAULT_API_KEY_ENV)
        self.assertEqual(client.routes["translation.body"].request_model().model, "deepseek-flash")
        self.assertEqual(client.routes["review.scan"].request_model().model, "deepseek-flash")
        self.assertTrue(client.routes["translation.body"].request_model().options.thinking)
        self.assertTrue(client.routes["synopsis.chapter"].request_model().options.thinking)

    def test_explicit_config_overrides_provider_defaults(self):
        client = RoutedLLMClient(_minimal_deepseek_cfg())

        self.assertEqual(client.adapter("default").base_url, "https://example.invalid/v1")
        self.assertEqual(client.adapter("default").api_key_env, "X")
        self.assertEqual(client.routes["translation.body"].request_model().model, "m1")

    def test_partial_tier_override_keeps_other_provider_defaults(self):
        client = RoutedLLMClient(
            model_config(
                profiles={
                    "fast": dict(
                        model="custom-fast",
                        options={"thinking": False},
                    ),
                }
            )
        )

        self.assertEqual(client.routes["synopsis.chapter"].request_model().model, "custom-fast")
        self.assertEqual(client.routes["translation.body"].request_model().model, "deepseek-flash")
        self.assertEqual(client.routes["review.scan"].request_model().model, "deepseek-flash")

    def test_provider_option_can_be_overridden_without_repeating_model(self):
        client = RoutedLLMClient(
            model_config(
                profiles={
                    "fast": dict(options={"thinking": True}),
                }
            )
        )

        self.assertEqual(client.routes["synopsis.chapter"].request_model().model, "deepseek-flash")
        self.assertTrue(client.routes["synopsis.chapter"].request_model().options.thinking)

    def test_unknown_provider_option_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_option"):
            RoutedLLMClient(
                model_config(
                    profiles={
                        "strong": dict(options={"unknown_option": True}),
                    }
                )
            )


class TestDeepSeekUsageByTier(unittest.TestCase):
    def test_records_usage_and_splits_by_tier(self):
        cfg = _minimal_deepseek_cfg()
        c = RoutedLLMClient(cfg)
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
        with patch.object(
            c.adapter("default"), "_ensure_client", return_value=_ClientStub(responses)
        ):
            self.assertEqual(c.complete(msgs, operation="translation.body"), "strong-out")
            self.assertEqual(c.complete(msgs, operation="review.scan"), "cheap-out")

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
        self.assertEqual(list(summary["by_stage"]), ["translation.body", "review.scan"])
        self.assertEqual(summary["by_stage"]["translation.body"]["total_tokens"], 1200)
        self.assertEqual(summary["by_stage"]["translation.body"]["cache_hit_rate"], 0.8)


class TestOpenAIUsageNormalization(unittest.TestCase):
    def test_nested_cached_tokens_are_normalized(self):
        cfg = model_config(
            kind="openai",
            base_url="https://example.invalid/v1",
            api_key_env="X",
            timeout=1,
            max_retries=0,
            profiles={"strong": dict(model="m")},
        )
        client = RoutedLLMClient(cfg)
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_tokens_details=SimpleNamespace(cached_tokens=40),
        )
        response = _make_response("ok", usage)

        with patch.object(
            client.adapter("default"),
            "_ensure_client",
            return_value=_ClientStub([response]),
        ):
            self.assertEqual(
                client.complete(
                    [{"role": "user", "content": "x"}],
                    operation="translation.body",
                ),
                "ok",
            )

        summary = client.usage_summary()
        self.assertEqual(summary["totals"]["cache_hit_tokens"], 40)
        self.assertEqual(summary["totals"]["cache_miss_tokens"], 60)
        self.assertEqual(summary["totals"]["cache_hit_rate"], 0.4)
        self.assertEqual(
            summary["by_stage"]["translation.body"]["cache_hit_rate"],
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
        c = RoutedLLMClient(cfg)
        with patch.object(
            c.adapter("default"),
            "_ensure_client",
            return_value=_ClientStub([_make_response("ok", None)]),
        ):
            self.assertEqual(
                c.complete([{"role": "user", "content": "x"}], operation="translation.body"), "ok"
            )
        summary = c.usage_summary()
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["totals"]["total_tokens"], 0)
        self.assertEqual(summary["by_tier"], {})
        self.assertEqual(summary["by_stage"], {})

    def test_complete_with_missing_usage_attr_does_not_count(self):
        cfg = _minimal_deepseek_cfg()
        c = RoutedLLMClient(cfg)
        msg = SimpleNamespace(content="ok")
        choice = SimpleNamespace(message=msg)
        # No usage attribute.
        resp = SimpleNamespace(choices=[choice])
        with patch.object(c.adapter("default"), "_ensure_client", return_value=_ClientStub([resp])):
            self.assertEqual(
                c.complete([{"role": "user", "content": "x"}], operation="translation.body"), "ok"
            )
        summary = c.usage_summary()
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["by_tier"], {})

    def test_missing_total_tokens_falls_back_to_prompt_plus_completion(self):
        tracker = UsageTracker()
        usage = _make_usage(prompt_tokens=40, completion_tokens=10)
        # Verify total_tokens is absent.
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
        agent = Agent(client, Config.from_dict({"llm": {"preset": "fake"}}))

        agent._ask_text("system", "user", operation="translation.body")
        agent._ask_json("system", "user", operation="review.scan", default=[])

        self.assertEqual(
            [call["stage"] for call in client.calls], ["translation.body", "review.scan"]
        )


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
        self._record(client, "strong", prompt=100, completion=20, stage="translation.body")
        first = client.usage_summary()
        self._record(client, "strong", prompt=50, completion=10, stage="translation.body")
        self._record(client, "fast", prompt=30, completion=5, stage="synopsis.chapter")
        second = client.usage_summary()

        increment = usage_delta(second, first)
        self.assertEqual(increment["totals"]["total_tokens"], 95)
        self.assertEqual(increment["by_stage"]["translation.body"]["total_tokens"], 60)
        self.assertEqual(increment["by_stage"]["synopsis.chapter"]["total_tokens"], 35)
        merged = merge_usage_summaries(first, increment)
        self.assertEqual(merged, second)

    def test_usage_accumulates_across_orchestrators_for_one_book(self):
        with tempfile.TemporaryDirectory() as d:
            store = RunStore(os.path.join(d, "state", "book"))
            config = Config.from_dict({"llm": {"preset": "fake"}})

            first_client = FakeClient()
            first = Orchestrator(config, client=first_client)
            self._record(
                first_client,
                "strong",
                prompt=100,
                completion=20,
                stage="translation.body",
            )
            cumulative = first._runtime.flush_usage(store, scope="translate")
            self.assertEqual(cumulative["totals"]["total_tokens"], 120)

            # A second flush in the same process without new calls must not double-count usage.
            unchanged = first._runtime.flush_usage(store, scope="pipeline")
            self.assertEqual(unchanged["totals"]["total_tokens"], 120)

            # Simulate resume with a new client/Orchestrator, adding deltas to the same book.
            resumed_client = FakeClient()
            resumed = Orchestrator(config, client=resumed_client)
            self._record(
                resumed_client,
                "cheap",
                prompt=40,
                completion=10,
                stage="review.scan",
            )
            cumulative = resumed._runtime.flush_usage(store, scope="translate")

            self.assertEqual(cumulative["totals"]["total_tokens"], 170)
            self.assertEqual(cumulative["totals"]["calls"], 2)
            self.assertEqual(cumulative["by_tier"]["strong"]["total_tokens"], 120)
            self.assertEqual(cumulative["by_tier"]["cheap"]["total_tokens"], 50)
            self.assertEqual(cumulative["by_stage"]["translation.body"]["total_tokens"], 120)
            self.assertEqual(cumulative["by_stage"]["review.scan"]["total_tokens"], 50)
            self.assertEqual(store.load_usage(), cumulative)
            self.assertTrue(os.path.isfile(store.usage_path))

    def test_report_omits_usage_and_usage_file_keeps_book_total(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "novel.txt")
            write_sample_txt(source)
            config = Config.from_dict(
                {
                    "language": {"source": "ja", "target": "zh"},
                    "llm": {"preset": "fake"},
                    "pipeline": {"book_understanding": False, "review": False},
                    "paths": {"state_dir": os.path.join(d, "state")},
                }
            )

            initial_client = FakeClient(handler=routing_handler)
            initial = Orchestrator(config, client=initial_client)
            store = initial.run_steps(source, {"translate"})["store"]
            self._record(initial_client, "strong", prompt=100, completion=20)
            initial._runtime.flush_usage(store, scope="translate")

            resumed_client = FakeClient(handler=routing_handler)
            resumed = Orchestrator(config, client=resumed_client)
            self._record(resumed_client, "cheap", prompt=40, completion=10)
            result = resumed.run_steps(source, {"report"})

            self.assertNotIn("usage", result["report"])
            usage = result["store"].load_usage()
            self.assertIsNotNone(usage)
            assert usage is not None
            self.assertEqual(usage["totals"]["total_tokens"], 170)
            self.assertEqual(usage["totals"]["calls"], 2)
            self.assertEqual(result["store"].load_usage(), usage)


class TestSourceIdentityAndExport(unittest.TestCase):
    @staticmethod
    def _config(directory: str) -> Config:
        return Config.from_dict(
            {
                "language": {"source": "ja", "target": "zh"},
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "fake-strong"}},
                },
                "pipeline": {
                    "book_understanding": False,
                    "review": False,
                    "polish": False,
                },
                "paths": {"state_dir": os.path.join(directory, "state")},
            }
        )

    def test_standalone_assemble_does_not_wait_for_active_translation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            output = os.path.join(directory, "translated.txt")
            write_sample_txt(source)
            config = self._config(directory)
            store = Orchestrator(
                config,
                client=_MeteredFakeClient(handler=routing_handler),
            ).run_steps(source, {"translate"})["store"]
            completed = threading.Event()
            errors: list[BaseException] = []

            def export() -> None:
                try:
                    Orchestrator(config, client=FakeClient()).run_assemble(
                        source,
                        out_format="txt",
                        out_path=output,
                    )
                except BaseException as error:  # pragma: no cover - asserted below
                    errors.append(error)
                finally:
                    completed.set()

            with store.lock():
                worker = threading.Thread(target=export)
                worker.start()
                completed_while_translation_locked = completed.wait(2)

            worker.join(timeout=2)
            self.assertTrue(completed_while_translation_locked)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(os.path.isfile(output))

    def test_assemble_only_run_steps_does_not_wait_for_active_translation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            output = os.path.join(directory, "translated.txt")
            write_sample_txt(source)
            config = self._config(directory)
            store = Orchestrator(
                config,
                client=_MeteredFakeClient(handler=routing_handler),
            ).run_steps(source, {"translate"})["store"]
            completed = threading.Event()
            errors: list[BaseException] = []

            def export() -> None:
                try:
                    Orchestrator(config, client=FakeClient()).run_steps(
                        source,
                        {"assemble"},
                        out_format="txt",
                        out_path=output,
                    )
                except BaseException as error:  # pragma: no cover - asserted below
                    errors.append(error)
                finally:
                    completed.set()

            with store.lock():
                worker = threading.Thread(target=export)
                worker.start()
                completed_while_translation_locked = completed.wait(2)

            worker.join(timeout=2)
            self.assertTrue(completed_while_translation_locked)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(os.path.isfile(output))

    def test_standalone_assemble_waits_for_another_export_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            output = os.path.join(directory, "translated.txt")
            write_sample_txt(source)
            config = self._config(directory)
            store = Orchestrator(
                config,
                client=_MeteredFakeClient(handler=routing_handler),
            ).run_steps(source, {"translate"})["store"]
            completed = threading.Event()
            errors: list[BaseException] = []

            def export() -> None:
                try:
                    Orchestrator(config, client=FakeClient()).run_assemble(
                        source,
                        out_format="txt",
                        out_path=output,
                    )
                except BaseException as error:  # pragma: no cover - asserted below
                    errors.append(error)
                finally:
                    completed.set()

            with store.assemble_lock():
                worker = threading.Thread(target=export)
                worker.start()
                self.assertFalse(completed.wait(0.1))

            self.assertTrue(completed.wait(2))
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(os.path.isfile(output))

    def test_changed_source_is_rejected_before_reusing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            write_sample_txt(source)
            config = self._config(directory)
            store = Orchestrator(
                config,
                client=_MeteredFakeClient(handler=routing_handler),
            ).run_steps(source, {"translate"})["store"]
            with open(source, "a", encoding="utf-8") as file:
                file.write("\n追加された本文。\n")

            with self.assertRaisesRegex(
                ValueError, "content does not match existing translation state"
            ):
                Orchestrator(config, client=FakeClient()).run_report(source)

            self.assertNotEqual(store.load_manifest()["source_sha256"], source_sha256(source))

    def test_same_size_source_change_with_restored_mtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            write_sample_txt(source)
            config = self._config(directory)
            Orchestrator(
                config,
                client=_MeteredFakeClient(handler=routing_handler),
            ).run_steps(source, {"translate"})
            resumed = Orchestrator(config, client=FakeClient())
            locate = resumed._preparation.locate_existing
            original_stat = os.stat(source)

            def mutate_then_locate(*args, **kwargs):
                with open(source, "rb") as file:
                    data = file.read()
                changed = data.replace("綾".encode(), "絢".encode(), 1)
                self.assertEqual(len(changed), len(data))
                with open(source, "wb") as file:
                    file.write(changed)
                os.utime(
                    source,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                return locate(*args, **kwargs)

            with (
                patch.object(
                    resumed._preparation,
                    "locate_existing",
                    side_effect=mutate_then_locate,
                ),
                self.assertRaisesRegex(
                    ValueError, "content does not match existing translation state"
                ),
            ):
                resumed.run_report(source)

    def test_existing_state_restores_detected_source_for_matching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            write_sample_txt(source)
            initial_config = self._config(directory)
            Orchestrator(
                initial_config,
                client=_MeteredFakeClient(handler=routing_handler),
            ).run_steps(source, {"translate"})
            resumed_config = self._config(directory)
            resumed_config.source_lang = "auto"
            resumed_config.target_lang = "zh"
            resumed = Orchestrator(resumed_config, client=FakeClient())

            resumed.run_report(source)

            self.assertEqual(resumed.config.source_lang, "ja")
            self.assertEqual(resumed.config.target_lang, "zh")
            self.assertEqual(resumed._runtime.reviewer.src, "ja")
            self.assertEqual(resumed._runtime.reviewer.tgt, "zh")

    def test_manifest_without_source_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "novel.txt")
            write_sample_txt(source)
            store = RunStore(os.path.join(directory, "state", "book"))
            store.save_manifest(
                {
                    "source_path": source,
                    "chapters": [],
                }
            )

            with self.assertRaisesRegex(ValueError, "has no valid source_sha256"):
                store.ensure_source_identity(source)


class TestRunStoreLock(unittest.TestCase):
    def test_export_snapshot_is_immutable_and_returns_chapter_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "book.txt")
            with open(source_path, "w", encoding="utf-8") as source:
                source.write("source")
            digest = source_sha256(source_path)
            chapter = Chapter(
                index=0,
                segments=[Segment(index=0, source="source", target="old translation")],
            )
            document = Document(
                title="Book",
                source_lang="en",
                target_lang="zh",
                fmt="txt",
                source_path=source_path,
                chapters=[chapter],
            )
            store = RunStore(os.path.join(directory, "state", "book"))
            manifest = store.stage_document(document, source_hash=digest)
            store.save_manifest(manifest)

            snapshot = store.create_export_snapshot(actual_sha256=digest)
            live_chapter = store.load_chapter(0)
            live_chapter.segments[0].target = "new translation"
            store.save_chapter(live_chapter)
            returned_chapter = snapshot.load_chapter(0)
            returned_chapter.segments[0].target = "mutated caller copy"

            self.assertEqual(
                snapshot.load_chapter(0).segments[0].target,
                "old translation",
            )
            self.assertEqual(store.load_chapter(0).segments[0].target, "new translation")

    def test_annotation_context_registry_is_stored_outside_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "book.epub")
            with open(source_path, "wb") as source:
                source.write(b"epub")
            registry = {
                "version": 1,
                "contexts": {
                    "notes.xhtml#n1": {
                        "source_blocks": ["A note."],
                    }
                },
            }
            document = Document(
                title="Book",
                source_lang="en",
                target_lang="zh",
                fmt="epub",
                source_path=source_path,
                chapters=[
                    Chapter(
                        index=0,
                        segments=[Segment(index=0, source="Body")],
                    )
                ],
                meta={
                    "epub_schema": 5,
                    "epub_annotation_contexts": registry,
                },
            )
            store = RunStore(os.path.join(directory, "state", "book"))

            manifest = store.stage_document(document, source_hash="a" * 64)

            self.assertNotIn("epub_annotation_contexts", manifest["meta"])
            self.assertEqual(store.load_annotation_contexts(), registry)
            store.begin_initialization("b" * 64)
            self.assertIsNone(store.load_annotation_contexts())

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

    def test_chapter_publish_waits_for_export_snapshot_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = os.path.join(directory, "state", "book")
            snapshot_reader = RunStore(run_dir)
            publisher = RunStore(run_dir)
            completed = threading.Event()
            chapter = Chapter(
                index=0,
                segments=[Segment(index=0, source="source", target="translation.body")],
            )

            def publish() -> None:
                publisher.save_chapter(chapter)
                completed.set()

            with snapshot_reader.state_lock():
                worker = threading.Thread(target=publish)
                worker.start()
                self.assertFalse(completed.wait(0.1))

            self.assertTrue(completed.wait(1))
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(publisher.load_chapter(0).segments[0].target, "translation.body")


if __name__ == "__main__":
    unittest.main()
