"""LLM 有效吞吐率、流水线 ETA 与 Rich 列的确定性测试。"""

from __future__ import annotations

import concurrent.futures
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rich.progress import Progress

from trans_novel.cli import _ETAColumn, _format_eta, _format_tokens, _RichProgressBridge
from trans_novel.config import LLMConfig, TierConfig
from trans_novel.llm.performance import PerformanceTracker, per_worker_token_rate
from trans_novel.llm.providers.deepseek import DeepSeekClient
from trans_novel.pipeline.eta import PipelineETAEstimator, ProgressEstimate


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TestPerformanceTracker(unittest.TestCase):
    def test_parallel_intervals_use_wall_clock_union(self):
        tracker = PerformanceTracker()
        tracker.record(
            provider="p",
            model="m",
            tier="fast",
            stage="Reviewer",
            completion_tokens=100,
            started_at=0,
            finished_at=10,
        )
        tracker.record(
            provider="p",
            model="m",
            tier="fast",
            stage="Reviewer",
            completion_tokens=100,
            started_at=0,
            finished_at=10,
        )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.token_rate, 20.0)
        self.assertEqual(per_worker_token_rate(snapshot.samples), 10.0)
        self.assertEqual(snapshot.total_tokens, 200)

    def test_concurrent_recording_is_exact(self):
        tracker = PerformanceTracker()

        def record(index: int) -> None:
            tracker.record(
                provider="p",
                model="m",
                tier="fast",
                stage="Synopsizer",
                completion_tokens=index + 1,
                started_at=float(index),
                finished_at=float(index + 1),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(record, range(32)))

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.total_calls, 32)
        self.assertEqual(snapshot.total_completion_tokens, sum(range(1, 33)))
        self.assertEqual(snapshot.total_tokens, sum(range(1, 33)))
        self.assertEqual(snapshot.last_sequence, 32)

    def test_zero_token_calls_do_not_displace_last_valid_rate_sample(self):
        tracker = PerformanceTracker(history_size=8)
        tracker.record(
            provider="p",
            model="m",
            tier="fast",
            stage="s",
            completion_tokens=1000,
            started_at=0,
            finished_at=1,
        )
        for index in range(20):
            tracker.record(
                provider="p",
                model="m",
                tier="fast",
                stage="s",
                completion_tokens=10,
                started_at=1 + index,
                finished_at=2 + index,
            )
        for index in range(8):
            tracker.record(
                provider="p",
                model="m",
                tier="fast",
                stage="s",
                completion_tokens=0,
                started_at=9 + index,
                finished_at=10 + index,
            )

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot.token_rate, 10.0)
        self.assertEqual(len(snapshot.samples), 8)


class TestProviderPerformance(unittest.TestCase):
    def test_openai_compatible_metric_includes_retry_time(self):
        clock = ManualClock()
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=10,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=usage,
        )

        class Completions:
            attempts = 0

            def create(self, **_kwargs):
                self.attempts += 1
                clock.advance(3 if self.attempts == 1 else 2)
                if self.attempts == 1:
                    raise TimeoutError("retry")
                return response

        stub = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        cfg = LLMConfig(
            provider="deepseek",
            base_url="https://example.test",
            max_retries=1,
            tiers={"strong": TierConfig(model="deepseek-test")},
        )
        client = DeepSeekClient(cfg)
        client.performance = PerformanceTracker(clock=clock)

        def retry_backoff(_retry_state):
            clock.advance(4)
            return 0

        with (
            patch.object(client, "_ensure_client", return_value=stub),
            patch(
                "trans_novel.llm.retrying.wait_for_provider_retry",
                new=retry_backoff,
            ),
        ):
            self.assertEqual(
                client.complete(
                    [{"role": "user", "content": "x"}],
                    tier="strong",
                    stage="Translator",
                ),
                "ok",
            )

        metric = client.performance_summary().samples[-1]
        self.assertEqual(metric.provider, "DeepSeek")
        self.assertEqual(metric.model, "deepseek-test")
        self.assertEqual(metric.stage, "Translator")
        self.assertEqual(metric.completion_tokens, 20)
        self.assertEqual(metric.prompt_tokens, 10)
        self.assertEqual(metric.total_tokens, 30)
        self.assertEqual(metric.elapsed_seconds, 9.0)
        self.assertAlmostEqual(client.performance_summary().token_rate or 0, 20 / 9)
        self.assertEqual(client.performance_summary().total_tokens, 30)

    def test_retry_usage_is_accumulated_for_token_budget(self):
        clock = ManualClock()
        first_usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=10,
        )
        final_usage = SimpleNamespace(
            prompt_tokens=20,
            completion_tokens=10,
            total_tokens=30,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=20,
        )
        class RetryableBrokenChoices:
            def __getitem__(self, _index):
                raise ConnectionError("response stream disconnected")

        responses = [
            SimpleNamespace(choices=RetryableBrokenChoices(), usage=first_usage),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=final_usage,
            ),
        ]

        class Completions:
            def create(self, **_kwargs):
                clock.advance(1)
                return responses.pop(0)

        stub = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        cfg = LLMConfig(
            provider="deepseek",
            base_url="https://example.test",
            max_retries=1,
            tiers={"strong": TierConfig(model="deepseek-test")},
        )
        client = DeepSeekClient(cfg)
        client.performance = PerformanceTracker(clock=clock)

        with (
            patch.object(client, "_ensure_client", return_value=stub),
            patch(
                "trans_novel.llm.retrying.wait_for_provider_retry",
                new=lambda _state: 0,
            ),
        ):
            self.assertEqual(client.complete([], tier="strong"), "ok")

        metric = client.performance_summary().samples[-1]
        self.assertEqual(metric.completion_tokens, 10)
        self.assertEqual(metric.prompt_tokens, 30)
        self.assertEqual(metric.total_tokens, 45)
        self.assertEqual(client.usage_summary()["totals"]["total_tokens"], 45)

    def test_missing_usage_records_elapsed_without_inventing_token_rate(self):
        clock = ManualClock()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        )

        class Completions:
            def create(self, **_kwargs):
                clock.advance(2)
                return response

        stub = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        cfg = LLMConfig(
            provider="deepseek",
            base_url="https://example.test",
            max_retries=0,
            tiers={"strong": TierConfig(model="deepseek-test")},
        )
        client = DeepSeekClient(cfg)
        client.performance = PerformanceTracker(clock=clock)

        with patch.object(client, "_ensure_client", return_value=stub):
            self.assertEqual(client.complete([], tier="strong"), "ok")

        snapshot = client.performance_summary()
        self.assertEqual(snapshot.total_calls, 1)
        self.assertEqual(snapshot.total_completion_tokens, 0)
        self.assertEqual(snapshot.total_tokens, 0)
        self.assertEqual(snapshot.samples[-1].elapsed_seconds, 2.0)
        self.assertIsNone(snapshot.token_rate)

    def test_failed_logical_call_does_not_publish_a_bogus_rate(self):
        clock = ManualClock()

        class Completions:
            def create(self, **_kwargs):
                clock.advance(1)
                raise TimeoutError("failed")

        stub = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        cfg = LLMConfig(
            provider="deepseek",
            base_url="https://example.test",
            max_retries=0,
            tiers={"strong": TierConfig(model="deepseek-test")},
        )
        client = DeepSeekClient(cfg)
        client.performance = PerformanceTracker(clock=clock)

        with (
            patch.object(client, "_ensure_client", return_value=stub),
            self.assertRaises(TimeoutError),
        ):
            client.complete([], tier="strong")

        snapshot = client.performance_summary()
        self.assertEqual(snapshot.total_calls, 0)
        self.assertIsNone(snapshot.token_rate)

    def test_failed_response_preserves_billable_usage_without_token_rate(self):
        clock = ManualClock()
        usage = SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=10,
        )
        response = SimpleNamespace(choices=[], usage=usage)

        class Completions:
            def create(self, **_kwargs):
                clock.advance(1)
                return response

        stub = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        cfg = LLMConfig(
            provider="deepseek",
            base_url="https://example.test",
            max_retries=0,
            tiers={"strong": TierConfig(model="deepseek-test")},
        )
        client = DeepSeekClient(cfg)
        client.performance = PerformanceTracker(clock=clock)

        with (
            patch.object(client, "_ensure_client", return_value=stub),
            self.assertRaises(IndexError),
        ):
            client.complete([], tier="strong")

        snapshot = client.performance_summary()
        self.assertEqual(snapshot.total_calls, 1)
        self.assertEqual(snapshot.total_completion_tokens, 0)
        self.assertEqual(snapshot.total_tokens, 15)
        self.assertIsNone(snapshot.token_rate)


class TestPipelineETAEstimator(unittest.TestCase):
    def test_new_estimator_does_not_reuse_previous_command_speed(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        clock.advance(1)
        tracker.record(
            provider="p",
            model="m",
            tier="strong",
            stage="old-command",
            completion_tokens=100,
            started_at=0,
        )

        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.begin_stage(
            "translate",
            label="翻译正文",
            total_work=100,
            kind="chars",
        )
        estimate = eta.snapshot()

        self.assertIsNone(estimate.token_rate)
        self.assertIsNone(estimate.stage_remaining_seconds)
        self.assertIsNone(estimate.overall_remaining_seconds)
        self.assertEqual(estimate.used_tokens, 0)
        self.assertIsNone(estimate.estimated_total_tokens)

    def test_first_response_produces_stage_and_overall_countdown(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.plan_stage("translate", 100, kind="chars", workers=1)
        eta.plan_stage("review:1", 10, kind="blocks", workers=2)
        eta.begin_stage(
            "translate",
            label="翻译正文",
            total_work=100,
            kind="chars",
            workers=1,
        )
        eta.set_active_work(10)

        started = clock()
        clock.advance(2)
        tracker.record(
            provider="p",
            model="m",
            tier="strong",
            stage="Translator",
            completion_tokens=20,
            prompt_tokens=80,
            total_tokens=100,
            started_at=started,
        )

        first = eta.snapshot()
        self.assertEqual(first.sample_count, 1)
        self.assertAlmostEqual(first.token_rate or 0, 10.0)
        self.assertAlmostEqual(first.stage_remaining_seconds or 0, 20.0)
        self.assertAlmostEqual(first.overall_remaining_seconds or 0, 30.0)
        self.assertEqual(first.used_tokens, 100)
        self.assertAlmostEqual(first.stage_remaining_tokens or 0, 900.0)
        self.assertAlmostEqual(first.overall_remaining_tokens or 0, 1900.0)
        self.assertAlmostEqual(first.estimated_total_tokens or 0, 2000.0)

        clock.advance(5)
        ticking = eta.snapshot()
        self.assertAlmostEqual(ticking.stage_remaining_seconds or 0, 15.0)
        self.assertAlmostEqual(ticking.overall_remaining_seconds or 0, 25.0)
        self.assertEqual(ticking.estimated_total_tokens, first.estimated_total_tokens)

    def test_missing_usage_falls_back_to_completed_work_speed(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.begin_stage(
            "translate",
            label="翻译正文",
            total_work=100,
            kind="chars",
        )
        clock.advance(10)
        eta.advance(10)

        estimate = eta.snapshot()
        self.assertIsNone(estimate.token_rate)
        self.assertAlmostEqual(estimate.stage_remaining_seconds or 0, 90.0)
        self.assertAlmostEqual(estimate.overall_remaining_seconds or 0, 90.0)
        self.assertEqual(estimate.used_tokens, 0)
        self.assertIsNone(estimate.estimated_total_tokens)

    def test_second_sample_recalibrates_deadlines(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.begin_stage("translate", label="翻译正文", total_work=100, kind="chars")

        eta.set_active_work(10)
        started = clock()
        clock.advance(2)
        tracker.record(
            provider="p",
            model="m",
            tier="strong",
            stage="Translator",
            completion_tokens=20,
            started_at=started,
        )
        eta.advance()
        first = eta.snapshot()
        self.assertAlmostEqual(first.stage_remaining_seconds or 0, 18.0)
        self.assertAlmostEqual(first.estimated_total_tokens or 0, 200.0)

        eta.set_active_work(10)
        started = clock()
        clock.advance(4)
        tracker.record(
            provider="p",
            model="m",
            tier="strong",
            stage="Translator",
            completion_tokens=20,
            started_at=started,
        )
        eta.advance()
        recalibrated = eta.snapshot()
        self.assertAlmostEqual(recalibrated.stage_remaining_seconds or 0, 24.0)
        self.assertAlmostEqual(recalibrated.estimated_total_tokens or 0, 200.0)

    def test_concurrent_stage_uses_observed_aggregate_throughput(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.begin_stage(
            "review:1",
            label="全书审校 R1",
            total_work=400,
            kind="chars",
            workers=2,
            tier="cheap",
        )
        clock.advance(10)
        for _ in range(2):
            tracker.record(
                provider="p",
                model="m",
                tier="cheap",
                stage="Reviewer",
                completion_tokens=100,
                started_at=0,
                finished_at=10,
            )
            eta.advance(100)

        estimate = eta.snapshot()
        self.assertAlmostEqual(estimate.token_rate or 0, 20.0)
        self.assertAlmostEqual(estimate.stage_remaining_seconds or 0, 10.0)
        self.assertEqual(estimate.used_tokens, 200)
        self.assertAlmostEqual(estimate.estimated_total_tokens or 0, 400.0)

    def test_stage_switch_clears_stage_eta_but_keeps_overall(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.plan_stage("translate", 10, kind="chars", tier="strong")
        eta.plan_stage("review:1", 10, kind="chars", tier="strong")
        eta.begin_stage(
            "translate",
            label="翻译正文",
            total_work=10,
            kind="chars",
            tier="strong",
        )
        eta.set_active_work(10)
        started = clock()
        clock.advance(1)
        tracker.record(
            provider="p",
            model="m",
            tier="strong",
            stage="Translator",
            completion_tokens=10,
            started_at=started,
        )
        eta.advance()
        eta.finish_stage("translate")
        eta.begin_stage(
            "review:1",
            label="全书审校 R1",
            total_work=10,
            kind="chars",
            tier="strong",
        )

        switched = eta.snapshot()
        self.assertIsNone(switched.stage_remaining_seconds)
        self.assertAlmostEqual(switched.overall_remaining_seconds or 0, 1.0)
        self.assertIsNone(switched.stage_remaining_tokens)
        self.assertAlmostEqual(switched.estimated_total_tokens or 0, 20.0)

    def test_future_stage_prefers_matching_tier_then_global_samples(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)

        eta.begin_stage("prescan", label="预扫", total_work=10, kind="chars", tier="fast")
        eta.set_active_work(10)
        started = clock()
        clock.advance(1)
        tracker.record(
            provider="p",
            model="fast-model",
            tier="fast",
            stage="Synopsizer",
            completion_tokens=10,
            started_at=started,
        )
        eta.advance()
        eta.finish_stage("prescan")

        eta.begin_stage("style", label="风格", total_work=10, kind="chars", tier="strong")
        eta.set_active_work(10)
        started = clock()
        clock.advance(10)
        tracker.record(
            provider="p",
            model="strong-model",
            tier="strong",
            stage="Analyzer",
            completion_tokens=100,
            started_at=started,
        )
        eta.advance()
        eta.finish_stage("style")
        eta.plan_stage("synopsis", 10, kind="chars", tier="fast")

        estimate = eta.snapshot()
        self.assertAlmostEqual(estimate.overall_remaining_seconds or 0, 1.0)
        self.assertAlmostEqual(estimate.estimated_total_tokens or 0, 120.0)

    def test_dynamic_branch_increases_overall_eta(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.begin_stage(
            "translate",
            label="翻译正文",
            total_work=100,
            kind="chars",
            tier="strong",
        )
        eta.set_active_work(10)
        started = clock()
        clock.advance(2)
        tracker.record(
            provider="p",
            model="m",
            tier="strong",
            stage="Translator",
            completion_tokens=20,
            started_at=started,
        )
        before = eta.snapshot().overall_remaining_seconds
        before_tokens = eta.snapshot().estimated_total_tokens
        eta.plan_stage("fix:1", 50, kind="chars", tier="strong")
        after = eta.snapshot().overall_remaining_seconds
        after_tokens = eta.snapshot().estimated_total_tokens

        assert before is not None and after is not None
        assert before_tokens is not None and after_tokens is not None
        self.assertGreater(after, before)
        self.assertGreater(after_tokens, before_tokens)

    def test_existing_progress_callback_signature_is_preserved(self):
        clock = ManualClock()
        eta = PipelineETAEstimator(PerformanceTracker(clock=clock), clock=clock)
        seen: list[tuple[int, int, str]] = []
        callback = eta.track(lambda done, total, label: seen.append((done, total, label)))
        assert callback is not None

        callback(2, 5, "审校")

        self.assertEqual(seen, [(2, 5, "审校")])

    def test_expired_deadline_never_becomes_negative(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.begin_stage("s", label="阶段", total_work=10, kind="chars")
        eta.set_active_work(5)
        started = clock()
        clock.advance(1)
        tracker.record(
            provider="p",
            model="m",
            tier="strong",
            stage="Translator",
            completion_tokens=5,
            started_at=started,
        )
        eta.snapshot()

        clock.advance(1000)
        expired = eta.snapshot()
        self.assertEqual(expired.stage_remaining_seconds, 0.0)
        self.assertEqual(expired.overall_remaining_seconds, 0.0)

    def test_local_finishing_does_not_reuse_stale_token_rate(self):
        clock = ManualClock()
        tracker = PerformanceTracker(clock=clock)
        eta = PipelineETAEstimator(tracker, clock=clock)
        eta.begin_stage("qa", label="一致性 QA", total_work=1, kind="calls")
        started = clock()
        clock.advance(1)
        tracker.record(
            provider="p",
            model="m",
            tier="cheap",
            stage="ConsistencyChecker",
            completion_tokens=10,
            started_at=started,
        )
        eta.advance(1)
        eta.finish_stage("qa")
        eta.mark_finishing()

        finishing = eta.snapshot()
        self.assertTrue(finishing.finishing)
        self.assertIsNone(finishing.token_rate)
        self.assertIsNone(finishing.stage_remaining_seconds)
        self.assertEqual(finishing.used_tokens, 10)
        self.assertEqual(finishing.estimated_total_tokens, 10.0)


class TestETAColumn(unittest.TestCase):
    def test_duration_format_is_compact_and_rounded_up(self):
        self.assertEqual(_format_eta(1), "<1m")
        self.assertEqual(_format_eta(61), "2m")
        self.assertEqual(_format_eta(3660), "1h01m")
        self.assertEqual(_format_eta(float("nan")), "--")
        self.assertEqual(_format_eta(float("inf")), "--")
        self.assertEqual(_format_tokens(999), "999")
        self.assertEqual(_format_tokens(12_345), "12.3k")
        self.assertEqual(_format_tokens(1_234_567), "1.23M")

    def test_column_renders_both_etas_and_rate(self):
        estimate = ProgressEstimate(
            stage_remaining_seconds=70,
            overall_remaining_seconds=130,
            token_rate=18.44,
            sample_count=1,
            updated_at=0,
            used_tokens=12_345,
            estimated_total_tokens=98_765,
        )
        column = _ETAColumn(lambda: estimate)
        progress = Progress(disable=True)
        progress.add_task("x", total=1)

        rendered = str(column.render(progress.tasks[0]))

        self.assertIn("阶段≈2m", rendered)
        self.assertIn("全程≈3m", rendered)
        self.assertIn("18.4 tok/s", rendered)
        self.assertIn("Token 已用 12.3k / 总≈98.8k", rendered)

    def test_column_distinguishes_calculating_and_finishing(self):
        progress = Progress(disable=True)
        progress.add_task("x", total=None)
        task = progress.tasks[0]
        calculating = ProgressEstimate(None, None, None, 0, 0)
        finishing = ProgressEstimate(
            None,
            None,
            None,
            0,
            0,
            finishing=True,
            used_tokens=1_200,
            estimated_total_tokens=1_200,
        )

        self.assertIn("计算中", str(_ETAColumn(lambda: calculating).render(task)))
        finishing_text = str(_ETAColumn(lambda: finishing).render(task))
        self.assertIn("收尾中", finishing_text)
        self.assertNotIn("tok/s", finishing_text)
        self.assertIn("Token 已用 1.2k / 总≈1.2k", finishing_text)

    def test_indeterminate_stage_switch_preserves_elapsed_time_and_one_row(self):
        clock = ManualClock()
        progress = Progress(disable=True, get_time=clock)
        bridge = _RichProgressBridge(progress, "准备中")
        clock.advance(12)
        bridge(1, 1, "阶段一")
        bridge(0, 0, "阶段二")

        self.assertEqual(len(progress.tasks), 1)
        self.assertEqual(progress.tasks[0].elapsed, 12.0)


if __name__ == "__main__":
    unittest.main()
