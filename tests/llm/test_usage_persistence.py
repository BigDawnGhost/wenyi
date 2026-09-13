"""Behavioral tests for eager schema-v2 usage persistence and recovery."""

from __future__ import annotations

import concurrent.futures
import errno
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.config import Config, LLMConfig, ModelRef
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.providers.transport import OpenAICompatibleTransport
from trans_novel.llm.usage import UsageTracker
from trans_novel.llm.usage_persistence import UsagePersistence, UsagePersistenceError
from trans_novel.pipeline import Application, build_workflow_definition
from trans_novel.pipeline.execution import WorkflowRunner
from trans_novel.pipeline.planning import PlanEntry, PlannedStage, WorkflowPlan
from trans_novel.pipeline.state import RunStore


def _usage(prompt: int, completion: int, reasoning: int = 0) -> SimpleNamespace:
    value = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )
    if reasoning:
        value.reasoning_tokens = reasoning
    return value


def _empty_response(usage: object) -> SimpleNamespace:
    message = SimpleNamespace(content="ok")
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _Completions:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self.response


class _Client:
    def __init__(self, response: object) -> None:
        self.completions = _Completions(response)
        self.chat = SimpleNamespace(completions=self.completions)


def _transport(tracker: UsageTracker) -> OpenAICompatibleTransport:
    config = LLMConfig.model_validate(
        {
            "models": {
                "translator": ["deepseek/m1"],
                "analyst": ["deepseek/m1"],
                "editor": ["deepseek/m1"],
                "fast": ["deepseek/m2"],
            }
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


class TestUsagePersistence(unittest.TestCase):
    def _bind(self, directory: str, tracker: UsageTracker) -> UsagePersistence:
        persistence = UsagePersistence()
        persistence.bind(os.path.join(directory, "usage.json"), tracker)
        persistence.begin_scope("translate")
        return persistence

    def test_response_is_on_disk_before_accounting_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            self._bind(directory, tracker)
            tracker.begin_attempt()
            tracker.record_attempt_result(
                agent="translator",
                operation="translate.batch",
                provider="deepseek",
                model_ref=ModelRef("deepseek", "m1"),
                usage=_usage(4, 2, reasoning=3),
            )
            with open(os.path.join(directory, "usage.json"), encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(persisted["totals"]["total_tokens"], 6)
            self.assertEqual(persisted["by_agent"]["translator"]["reasoning_tokens"], 3)
            self.assertEqual(persisted["by_operation"]["translate.batch"]["reasoning_tokens"], 3)

    def test_windows_directory_handles_do_not_block_usage_commit(self):
        from trans_novel.llm import usage_persistence

        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            self._bind(directory, tracker)
            windows_os = SimpleNamespace(**vars(os))
            windows_os.name = "nt"
            with (
                patch.object(usage_persistence, "os", windows_os),
                patch.object(
                    windows_os,
                    "open",
                    side_effect=PermissionError(errno.EACCES, "directory handle unavailable"),
                ),
            ):
                tracker.begin_attempt()
                tracker.record_attempt_result(
                    agent="translator", operation="translate.batch", usage=_usage(4, 2)
                )
            with open(os.path.join(directory, "usage.json"), encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertEqual(persisted["totals"]["total_tokens"], 6)
            self.assertEqual(persisted["totals"]["calls"], 1)
            self.assertFalse(os.path.exists(os.path.join(directory, "usage.wal.json")))

    def test_crash_before_usage_replace_recovers_after_once(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            persistence = self._bind(directory, tracker)
            tracker.begin_attempt()
            with (
                patch.object(persistence, "_replace_usage", side_effect=OSError("crash")),
                self.assertRaises(UsagePersistenceError),
            ):
                tracker.record_attempt_result(
                    agent="translator",
                    operation="translate.batch",
                    usage=_usage(4, 2),
                )
            recovered = UsagePersistence()
            resumed = UsageTracker()
            recovered.bind(os.path.join(directory, "usage.json"), resumed)
            with open(os.path.join(directory, "usage.json"), encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["totals"]["total_tokens"], 6)
            self.assertFalse(os.path.exists(os.path.join(directory, "usage.wal.json")))

    def test_crash_after_usage_replace_clears_wal_without_doubling(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            persistence = self._bind(directory, tracker)
            tracker.begin_attempt()
            with (
                patch.object(persistence, "_clear_wal", side_effect=OSError("crash")),
                self.assertRaises(UsagePersistenceError),
            ):
                tracker.record_attempt_result(
                    agent="translator",
                    operation="translate.batch",
                    usage=_usage(4, 2),
                )
            recovered = UsagePersistence()
            resumed = UsageTracker()
            recovered.bind(os.path.join(directory, "usage.json"), resumed)
            with open(os.path.join(directory, "usage.json"), encoding="utf-8") as stream:
                value = json.load(stream)
            self.assertEqual(value["totals"]["calls"], 1)
            self.assertFalse(os.path.exists(os.path.join(directory, "usage.wal.json")))

    def test_wal_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            persistence = self._bind(directory, tracker)
            tracker.begin_attempt()
            with (
                patch.object(persistence, "_clear_wal", side_effect=OSError("crash")),
                self.assertRaises(UsagePersistenceError),
            ):
                tracker.record_attempt_result(
                    agent="translator",
                    operation="translate.batch",
                    usage=_usage(4, 2),
                )
            with open(os.path.join(directory, "usage.json"), "w", encoding="utf-8") as stream:
                json.dump({"schema_version": 2, "totals": {}}, stream)
            with self.assertRaises(UsagePersistenceError):
                UsagePersistence().bind(os.path.join(directory, "usage.json"), UsageTracker())

    def test_wal_without_tracker_checkpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            persistence = self._bind(directory, tracker)
            tracker.begin_attempt()
            with (
                patch.object(persistence, "_clear_wal", side_effect=OSError("crash")),
                self.assertRaises(UsagePersistenceError),
            ):
                tracker.record_attempt_result(
                    agent="translator",
                    operation="translate.batch",
                    usage=_usage(4, 2),
                )
            wal_path = os.path.join(directory, "usage.wal.json")
            with open(wal_path, encoding="utf-8") as stream:
                wal = json.load(stream)
            wal.pop("tracker_after")
            with open(wal_path, "w", encoding="utf-8") as stream:
                json.dump(wal, stream)
            with self.assertRaises(UsagePersistenceError):
                UsagePersistence().bind(os.path.join(directory, "usage.json"), UsageTracker())

    def test_request_failure_records_attempt_without_inventing_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            self._bind(directory, tracker)
            transport = _transport(tracker)

            class _Failure:
                def create(self, **kwargs):
                    raise RuntimeError("down")

            transport._client = SimpleNamespace(chat=SimpleNamespace(completions=_Failure()))
            with self.assertRaisesRegex(RuntimeError, "down"):
                transport.complete(
                    [{"role": "user", "content": "x"}],
                    ModelRef("deepseek", "m1"),
                    agent="translator",
                    operation="translate.batch",
                )
            with open(os.path.join(directory, "usage.json"), encoding="utf-8") as stream:
                persisted = json.load(stream)
            self.assertEqual(persisted["totals"]["calls"], 0)
            self.assertEqual(persisted["by_operation"]["translate.batch"]["attempts"], 1)
            self.assertEqual(persisted["by_operation"]["translate.batch"]["failed_attempts"], 1)

    def test_agent_does_not_swallow_persistence_error_as_default(self):
        from trans_novel.agents.base import Agent

        class _PersistenceFailureClient:
            def complete(self, *args, **kwargs):
                raise UsagePersistenceError("disk full")

            def complete_json(self, *args, **kwargs):
                raise UsagePersistenceError("disk full")

        agent = Agent(
            _PersistenceFailureClient(),
            Config.from_dict({"llm": fake_llm_dict()}),
        )
        with self.assertRaises(UsagePersistenceError):
            agent._ask_text(
                "sys",
                "user",
                agent="translator",
                operation="translate.batch",
                default="fallback",
            )
        with self.assertRaises(UsagePersistenceError):
            agent._ask_json(
                "sys",
                "user",
                agent="translator",
                operation="translate.batch",
                default={},
            )

    def test_concurrent_attempts_have_exact_schema_v2_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            self._bind(directory, tracker)

            def account(_: int) -> None:
                tracker.begin_attempt()
                tracker.record_attempt_result(
                    agent="translator", operation="translate.batch", usage=_usage(2, 1)
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(account, range(12)))
            with open(os.path.join(directory, "usage.json"), encoding="utf-8") as stream:
                value = json.load(stream)
            self.assertEqual(value["schema_version"], 2)
            self.assertEqual(value["totals"]["calls"], 12)
            self.assertEqual(value["totals"]["total_tokens"], 36)

    def test_existing_schema_v2_and_sequential_books_do_not_cross_attribute(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            persistence = UsagePersistence()
            first = os.path.join(directory, "first", "usage.json")
            second = os.path.join(directory, "second", "usage.json")
            persistence.bind(first, tracker)
            tracker.begin_attempt()
            tracker.record_attempt_result(
                agent="translator", operation="translate.batch", usage=_usage(4, 2)
            )
            persistence.begin_scope("translate")
            persistence.finish_scope("translate")
            persistence.bind(second, tracker)
            tracker.begin_attempt()
            tracker.record_attempt_result(
                agent="translator", operation="translate.batch", usage=_usage(1, 1)
            )
            with open(second, encoding="utf-8") as stream:
                value = json.load(stream)
            self.assertEqual(value["totals"]["calls"], 1)
            self.assertEqual(value["totals"]["total_tokens"], 2)

    def test_runner_does_not_continue_after_best_effort_persistence_failure(self):
        class _FailingNode:
            node_id = "mine_terms"
            scope = "book"

            def execute(self, request):
                raise UsagePersistenceError("disk full")

        with tempfile.TemporaryDirectory() as directory:
            runner = WorkflowRunner(
                definition=build_workflow_definition(),
                node_factory=lambda node_id, ci: _FailingNode(),
            )
            plan = WorkflowPlan(
                stages=[PlannedStage(entries=[PlanEntry("mine_terms", "mine_terms", None, "book")])]
            )
            with self.assertRaises(UsagePersistenceError):
                runner.run(plan, store=RunStore(directory), input_path="")

    def test_glossary_audit_binds_usage_to_supplied_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            app = Application(
                Config.from_dict({"llm": fake_llm_dict()}),
                client=FakeClient(),
            )

            def fake_audit(store, glossary, auditor):
                auditor.client.complete(
                    [{"role": "user", "content": "audit"}],
                    agent="analyst",
                    operation="glossary.audit",
                )
                return []

            with patch("trans_novel.pipeline.application.audit_glossary", side_effect=fake_audit):
                self.assertEqual(app.glossary_audit(store), [])
            persisted = store.load_usage()
            self.assertEqual(persisted["by_operation"]["glossary.audit"]["attempts"], 1)

    def test_scope_event_contains_only_increment(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            tracker = UsageTracker()
            persistence = UsagePersistence()
            persistence.bind(
                os.path.join(directory, "usage.json"),
                tracker,
                event_callback=lambda scope, increment: events.append((scope, increment)),
            )
            persistence.begin_scope("translate")
            tracker.begin_attempt()
            tracker.record_attempt_result(
                agent="translator", operation="translate.batch", usage=_usage(1, 1)
            )
            persistence.finish_scope("translate")
        self.assertEqual(events[0][0], "translate")
        self.assertEqual(events[0][1]["schema_version"], 2)
        self.assertNotIn("cumulative", events[0][1])


if __name__ == "__main__":
    unittest.main()
