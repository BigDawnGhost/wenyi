"""Offline end-to-end orchestration and resume tests using FakeClient."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from wenyi_core.agents.reviewer import ReviewOutputError
from wenyi_core.glossary.store import GlossaryStore
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.usage import UsageSample
from wenyi_core.pipeline.orchestrator import Orchestrator

from tests.fake_llm import MeteredFakeClient, routing_handler
from tests.pipeline_fixtures import fake_pipeline_config as _config
from tests.pipeline_fixtures import require_file_storage
from tests.review_fixtures import fix_json as _fix_json
from tests.review_fixtures import review_json as _review_json
from tests.sample_data import write_sample_txt


class TestReviewReporting(unittest.TestCase):
    """Read-only review preserves formal text while persisting results, events and usage."""

    def _handler(self):
        """Report an omission at index zero in every review block and route other calls
        normally.
        """

        def handler(messages, tier, json_mode):
            sys = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in sys:
                return _review_json(
                    user,
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "漏了一句",
                            "suggestion": "补上",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        return handler

    def _run(self, d):
        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))
        orch = Orchestrator(cfg, client=FakeClient(handler=self._handler()))
        orch.run(txt)
        return orch.run_review(txt)

    @staticmethod
    def _load_internal_issues(result):
        """Read complete round issue records for logical assertions only."""
        return json.loads(
            Path(
                result["review_dir"],
                "rounds/final/unresolved_issues.json",
            ).read_text(encoding="utf-8")
        )

    def test_run_does_not_call_reviewer_even_for_only_chapter(self):
        """Body translation and only_chapter must not implicitly run final review."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            store = require_file_storage(Orchestrator(cfg, client=client).run(txt, only_chapter=0))
            Orchestrator(cfg, client=client).run(txt)

            review_calls = [
                call
                for call in client.calls
                if "translation reviewer" in call["messages"][0]["content"]
            ]
            self.assertEqual(review_calls, [])
            self.assertTrue(
                all("review_status" not in chapter for chapter in store.load_manifest()["chapters"])
            )

    def test_review_never_modifies_body_or_translation_state(self):
        """Review preserves formal state and historical project reports."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = MeteredFakeClient(handler=self._handler())
            orch = Orchestrator(cfg, client=client)
            store = require_file_storage(orch.run(txt))
            legacy_report = Path(store.run_dir) / "report.json"
            legacy_report.write_text('{"legacy": true}', encoding="utf-8")
            watched = [
                store.manifest_path,
                store.chapter_path(0),
                store.glossary_path,
                str(legacy_report),
            ]
            before = {
                path: Path(path).read_bytes() if os.path.exists(path) else None for path in watched
            }
            usage_before = Path(store.usage_path).read_bytes()
            events_before = Path(store.event_log_path).read_bytes()

            result = orch.run_review(txt)
            store = result["store"]
            chapter = store.load_chapter(0)
            self.assertTrue(result["review_issues"])
            self.assertEqual(chapter.meta.get("review_issues", []), [])
            self.assertEqual(
                before,
                {
                    path: Path(path).read_bytes() if os.path.exists(path) else None
                    for path in watched
                },
            )
            self.assertNotEqual(Path(store.usage_path).read_bytes(), usage_before)
            self.assertNotEqual(Path(store.event_log_path).read_bytes(), events_before)
            self.assertTrue(os.path.isfile(os.path.join(result["review_dir"], "result.json")))
            with open(
                os.path.join(result["review_dir"], "usage.json"),
                encoding="utf-8",
            ) as file:
                review_usage = json.load(file)
            self.assertGreater(review_usage["totals"]["calls"], 0)
            self.assertIn("review.scan", review_usage["by_stage"])
            self.assertNotIn("translation.body", review_usage["by_stage"])
            self.assertIn("review.scan", (store.load_usage() or {})["by_stage"])
            self.assertGreater(
                client.usage_summary()["by_stage"]["review.scan"]["calls"],
                0,
            )

    def test_review_saves_unified_result_and_round_diagnostics(self):
        with tempfile.TemporaryDirectory() as d:
            result = self._run(d)
            with open(
                os.path.join(
                    result["review_dir"],
                    "rounds/final/initial_issues.json",
                ),
                encoding="utf-8",
            ) as file:
                initial = json.load(file)
            with open(
                os.path.join(result["review_dir"], "result.json"),
                encoding="utf-8",
            ) as file:
                saved = json.load(file)
            self.assertTrue(initial)
            self.assertTrue(saved["issues"])
            self.assertEqual(saved, result["review_result"])
            self.assertEqual(
                set(saved["issues"][0]),
                {"issue_key", "chapter", "index", "type", "detail", "suggestion"},
            )
            self.assertEqual(
                set(os.listdir(result["review_dir"])),
                {
                    "events.jsonl",
                    "result.json",
                    "rounds",
                    "usage.json",
                    "chunks",
                    "checkpoint.json",
                    "created.json",
                },
            )

    def test_review_only_run_steps_returns_formal_read_only_result(self):
        """Internal review-only steps match the standalone command and preserve formal text."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=MeteredFakeClient(handler=self._handler()))
            store = require_file_storage(orch.run(txt))
            manifest_before = Path(store.manifest_path).read_bytes()
            chapter_before = Path(store.chapter_path(0)).read_bytes()

            result = orch.run_steps(txt, {"review"})

            self.assertEqual(Path(store.manifest_path).read_bytes(), manifest_before)
            self.assertEqual(Path(store.chapter_path(0)).read_bytes(), chapter_before)
            self.assertIsNone(result["output"])
            self.assertEqual(result["outputs"], [])
            self.assertTrue(result["review_issues"])
            self.assertTrue(os.path.isfile(os.path.join(result["review_dir"], "result.json")))

    def test_independent_review_does_not_create_report_implicitly(self):
        with tempfile.TemporaryDirectory() as d:
            result = self._run(d)
            self.assertFalse((Path(result["store"].run_dir) / "report.json").exists())

    def test_review_index_mapping(self):
        """Map block-local issue indices correctly to chapter-local positions."""

        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_tokens_per_batch = (
                8  # A review budget of 24 gives each paragraph its own block.
            )
            cfg.pipeline.review_agent_loop = False
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            result = orch.run_review(txt)
            idxs = sorted(
                i["index"]
                for i in result["review_issues"]
                if i.get("chapter") == 0 and i.get("type") == "missing"
            )
            segment_count = len(result["store"].load_chapter(0).text_segments)
            # Local index zero must map to a distinct chapter position for each block.
            self.assertEqual(idxs, list(range(segment_count)))

    def test_review_progress_advances_per_chunk_and_resets_for_blind_round(self):
        """Review advances paragraph progress by block; blind rechecks and clean confirmation
        have separate stages.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_tokens_per_batch = (
                8  # Split each chapter into multiple top-level review blocks.
            )
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 2
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)
            events: list[tuple[int, int, str]] = []

            orch.run_review(
                txt,
                progress=lambda done, total, label, payload=None: events.append(
                    (done, total, label)
                ),
            )

        first = [
            (done, total)
            for done, total, label in events
            if label == "progress.stage.reviewRound:1"
        ]
        second = [
            (done, total)
            for done, total, label in events
            if label == "progress.stage.reviewRound:2"
        ]
        clean = [(done, total) for done, total, label in events if label == "Clean confirmation"]
        self.assertGreater(len(first), 2)
        self.assertGreater(len(second), 2)
        for stage in (first, second):
            self.assertEqual(stage[0][0], 0)
            self.assertEqual(stage[-1][0], stage[-1][1])
            self.assertEqual([done for done, _ in stage], sorted(done for done, _ in stage))
            self.assertTrue(any(0 < done < total for done, total in stage))
        self.assertEqual(clean, [(1, 2), (2, 2)])
        loading = [
            (done, total)
            for done, total, label in events
            if label == "progress.stage.reviewChapters"
        ]
        self.assertTrue(loading)
        self.assertEqual(loading[0][0], 0)
        self.assertEqual(loading[-1][0], loading[-1][1])
        labels = [label for _, _, label in events]
        self.assertLess(
            labels.index("progress.stage.reviewCheckpoint"),
            labels.index("progress.stage.reviewRound:1"),
        )

    def test_review_accepts_numeric_string_index(self):
        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": "0",
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_agent_loop = False

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            result = orch.run_review(txt)

            issues = result["review_issues"]
            self.assertTrue(issues)
            self.assertEqual(issues[0]["index"], 0)

    def test_review_rejects_invalid_index_instead_of_returning_zero(self):
        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": "unknown",
                            "type": "missing",
                            "detail": "x",
                            "suggestion": "补译",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_output_retries = 0
            cfg.segment.max_tokens_per_batch = 100_000

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            with pytest.raises(ReviewOutputError, match="invalid_issue_index"):
                orch.run_review(txt)

    def test_review_skips_when_already_completed_with_same_content(self):
        """Automatically reuse completed review when content fingerprints match."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            first = orch.run_review(txt)
            first_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )
            self.assertGreater(first_count, 0)

            second = orch.run_review(txt)
            second_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )
            # Unchanged content skips new LLM calls.
            self.assertEqual(second_count, first_count)
            # Reuse the same result.
            self.assertEqual(first["review_dir"], second["review_dir"])

    def test_review_reruns_when_review_config_changed(self):
        """Changed review configuration invalidates completed results even when content is
        unchanged.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            first = orch.run_review(txt)
            first_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )

            cfg.pipeline.review_agent_max_evidence_rounds = 1  # Change configuration.
            orch2 = Orchestrator(cfg, client=client)
            second = orch2.run_review(txt)
            second_count = sum(
                "translation reviewer" in call["messages"][0]["content"] for call in client.calls
            )
            self.assertGreater(second_count, first_count)  # Run review again.
            self.assertNotEqual(first["review_dir"], second["review_dir"])

    def test_review_balance_error_stays_resumable(self):
        """Provider balance/quota stops must keep Review interrupted and resumable."""

        class BalanceError(Exception):
            def __init__(self) -> None:
                super().__init__("Error code: 402 - Insufficient Balance")
                self.status_code = 402

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_autofix = False
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)

            with patch.object(orch._review._chunks, "review_chapters", side_effect=BalanceError()):
                with self.assertRaises(BalanceError):
                    orch.run_review(txt)

            book_root = Path(cfg.state_dir)
            review_dirs = sorted(book_root.glob("*/targets/*/reviews/review-*"))
            self.assertEqual(len(review_dirs), 1)
            result_path = review_dirs[0] / "result.json"
            with open(result_path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(state["termination"], "interrupted")
            self.assertEqual(state["last_error"]["type"], "BalanceError")

            resumed = orch.run_review(txt)
            self.assertEqual(Path(resumed["review_dir"]).resolve(), review_dirs[0].resolve())
            with open(result_path, encoding="utf-8") as handle:
                finished = json.load(handle)
            self.assertEqual(finished["status"], "completed")

    def test_review_permanent_error_finishes_failed_but_remains_resumable(self):
        """Local/protocol failures stay logged as failed, yet resume the same directory."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_autofix = False
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)

            with patch.object(
                orch._review._chunks, "review_chapters", side_effect=ValueError("bad block")
            ):
                with self.assertRaises(ValueError):
                    orch.run_review(txt)

            book_root = Path(cfg.state_dir)
            first_dirs = sorted(book_root.glob("*/targets/*/reviews/review-*"))
            self.assertEqual(len(first_dirs), 1)
            result_path = first_dirs[0] / "result.json"
            with open(result_path, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["termination"], "error")
            self.assertEqual(state["error"]["type"], "ValueError")

            second = orch.run_review(txt)
            self.assertEqual(Path(second["review_dir"]).resolve(), first_dirs[0].resolve())
            with open(result_path, encoding="utf-8") as handle:
                finished = json.load(handle)
            self.assertEqual(finished["status"], "completed")
            self.assertNotIn("error", finished)

    def test_review_running_resume_rejects_config_change(self):
        """Changed configuration must start a new review directory instead of resuming stale
        running state.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            orch.run(txt)

            first = orch.run_review(txt)
            review_dir = first["review_dir"]
            result_path = os.path.join(review_dir, "result.json")
            with open(result_path, encoding="utf-8") as f:
                state = json.load(f)
            state["status"] = "running"
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            checkpoint = os.path.join(review_dir, "checkpoint.json")
            if os.path.isfile(checkpoint):
                os.remove(checkpoint)

            cfg.pipeline.review_agent_max_evidence_rounds = 1
            orch2 = Orchestrator(cfg, client=client)
            second = orch2.run_review(txt)
            self.assertNotEqual(first["review_dir"], second["review_dir"])

    def test_review_resume_reuses_initial_and_agent_traces(self):
        """After deleting a chunk cache, reuse initial and agent traces without new model
        calls.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=self._handler()))
            orch.run(txt)
            result = orch.run_review(txt)

            # Simulate interruption by resetting a completed review to running and removing its round checkpoint.
            # A real mid-scan interruption has no checkpoint and resumes scanning from round one.
            review_dir = result["review_dir"]
            result_path = os.path.join(review_dir, "result.json")
            with open(result_path, encoding="utf-8") as f:
                state = json.load(f)
            state["status"] = "running"
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            checkpoint = os.path.join(review_dir, "checkpoint.json")
            if os.path.isfile(checkpoint):
                os.remove(checkpoint)

            # Remove the first chunk cache but retain its initial/agent traces.
            chunks_dir = os.path.join(review_dir, "chunks")
            chunk_files = sorted(os.listdir(chunks_dir))
            self.assertTrue(chunk_files)
            removed = os.path.join(chunks_dir, chunk_files[0])
            removed_bytes = Path(removed).read_bytes()
            os.remove(removed)

            meter = MeteredFakeClient(handler=self._handler())
            orch2 = Orchestrator(cfg, client=meter)
            with patch.object(
                GlossaryStore, "terms_in", wraps=GlossaryStore.terms_in
            ) as match_terms:
                orch2.run_review(txt)
            match_terms.assert_not_called()

            reused_stages = [
                call["stage"]
                for call in meter.calls
                if call["stage"] in ("review.scan", "review.verify")
            ]
            self.assertEqual(reused_stages, [])
            self.assertTrue(os.path.isfile(removed))  # Persist the chunk again.
            # Reused output must match the first run byte for byte.
            self.assertEqual(Path(removed).read_bytes(), removed_bytes)
            # The finished-agent fast path does not emit review_agent_resumed in this test;
            # verify that the persisted event stream remains readable.
            with open(os.path.join(review_dir, "events.jsonl"), encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            self.assertTrue(any(e["event"] == "review_leaf_finished" for e in events))

    def test_review_rejects_incomplete_book(self):
        """Standalone final review requires every chapter to be translated."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            store = require_file_storage(orch.run(txt, only_chapter=0))

            with pytest.raises(ValueError, match="requires every chapter to be translated"):
                orch.run_review(txt)

            self.assertFalse(os.path.exists(store.reviews_dir))

    def test_review_without_state_rejects_pdf_before_conversion(self):
        """Reviewing a PDF without state must not convert it or create an empty state
        directory.
        """
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "book.pdf")
            with open(pdf, "wb") as file:
                file.write(b"%PDF-1.4\n")
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)

            with (
                patch("wenyi_core.pipeline.prepare.input_preparation.load_document") as loader,
                pytest.raises(ValueError, match="No translation progress found"),
            ):
                orch.run_review(pdf)

            loader.assert_not_called()
            self.assertEqual(client.calls, [])
            self.assertFalse(os.path.exists(cfg.state_dir))

    def test_review_without_state_does_not_initialize_text_book(self):
        """Without state, ordinary input may be located locally but must not trigger analysis
        or initialization.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            with pytest.raises(ValueError, match="No translation progress found"):
                Orchestrator(cfg, client=client).run_review(txt)

            self.assertEqual(client.calls, [])
            self.assertFalse(os.path.exists(cfg.state_dir))

    def test_reviewer_failure_keeps_body_and_writes_failed_review_result(self):
        """Service failures preserve body text but persist failed results, events and usage."""

        def handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                raise RuntimeError("review service unavailable")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_tokens_per_batch = 100_000
            cfg.pipeline.review_concurrency = 1
            client = MeteredFakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)
            store = require_file_storage(orch.run(txt))
            usage_before = Path(store.usage_path).read_bytes()
            events_before = Path(store.event_log_path).read_bytes()

            with pytest.raises(RuntimeError, match="review service unavailable"):
                orch.run_review(txt)

            review_calls = [
                call
                for call in client.calls
                if "translation reviewer" in call["messages"][0]["content"]
            ]
            # Recover only output protocol errors; splitting must not multiply retries for service failures.
            self.assertEqual(len(review_calls), 1)
            self.assertTrue(
                all("review_status" not in chapter for chapter in store.load_manifest()["chapters"])
            )
            runs = sorted(os.listdir(store.reviews_dir))
            with open(
                os.path.join(store.reviews_dir, runs[-1], "result.json"),
                encoding="utf-8",
            ) as file:
                receipt = json.load(file)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["termination"], "error")
            self.assertEqual(receipt["error"]["type"], "RuntimeError")
            with open(
                os.path.join(store.reviews_dir, runs[-1], "usage.json"),
                encoding="utf-8",
            ) as file:
                review_usage = json.load(file)
            self.assertEqual(review_usage["totals"]["calls"], 1)
            self.assertEqual(review_usage["totals"]["total_tokens"], 8)
            self.assertEqual(review_usage["by_stage"]["review.scan"]["calls"], 1)
            self.assertNotEqual(Path(store.usage_path).read_bytes(), usage_before)
            self.assertNotEqual(Path(store.event_log_path).read_bytes(), events_before)
            self.assertEqual((store.load_usage() or {})["by_stage"]["review.scan"]["calls"], 1)
            self.assertEqual(client.usage_summary()["by_stage"]["review.scan"]["calls"], 1)

    def test_review_interrupt_saves_usage_and_resumes_without_recounting(self):
        """A KeyboardInterrupt after one completed reviewer response must flush the review
        usage ledger and review_interrupted event, and a fresh orchestrator must resume
        the same run dir without counting any call twice.
        """

        def interrupting_handler(messages, tier, json_mode):
            if "translation reviewer" in messages[0]["content"]:
                if reviewer_calls:
                    raise KeyboardInterrupt
                reviewer_calls.append("answered")
                return _review_json(
                    messages[-1]["content"],
                    [
                        {
                            "index": 0,
                            "type": "missing",
                            "detail": "漏了一句",
                            "suggestion": "补上",
                        }
                    ],
                )
            return routing_handler(messages, tier, json_mode)

        reviewer_calls: list[str] = []
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_concurrency = 1
            client1 = MeteredFakeClient(handler=interrupting_handler)
            orch1 = Orchestrator(cfg, client=client1)
            store = require_file_storage(orch1.run(txt))
            translated_calls = len(client1.calls)

            with self.assertRaises(KeyboardInterrupt):
                orch1.run_review(txt)

            review_dir = os.path.join(store.reviews_dir, sorted(os.listdir(store.reviews_dir))[-1])
            run1_review_stages = [
                call["stage"]
                for call in client1.calls[translated_calls:]
                if call["stage"].startswith("review.")
            ]
            # scan issue → verify → second scan (KeyboardInterrupt after transport billed it).
            self.assertEqual(
                run1_review_stages,
                ["review.scan", "review.verify", "review.scan"],
            )
            run1_review_calls = len(run1_review_stages)
            with open(os.path.join(review_dir, "usage.json"), encoding="utf-8") as file:
                interrupted_usage = json.load(file)
            self.assertEqual(interrupted_usage["totals"]["calls"], run1_review_calls)
            with open(os.path.join(review_dir, "events.jsonl"), encoding="utf-8") as file:
                events = [json.loads(line)["event"] for line in file if line.strip()]
            self.assertIn("review_interrupted", events)

            client2 = MeteredFakeClient(handler=self._handler())
            result = Orchestrator(cfg, client=client2).run_review(txt)

            self.assertEqual(result["review_dir"], review_dir)
            self.assertEqual(result["review_result"]["status"], "completed")
            with open(os.path.join(review_dir, "events.jsonl"), encoding="utf-8") as file:
                events = [json.loads(line)["event"] for line in file if line.strip()]
            self.assertIn("review_resumed_from_checkpoint", events)
            run2_review_calls = sum(call["stage"].startswith("review.") for call in client2.calls)
            with open(os.path.join(review_dir, "usage.json"), encoding="utf-8") as file:
                resumed_usage = json.load(file)
            self.assertEqual(
                resumed_usage["totals"]["calls"],
                run1_review_calls + run2_review_calls,
            )
            book_usage = store.load_usage()
            assert book_usage is not None
            self.assertEqual(
                book_usage["totals"]["calls"],
                len(client1.calls) + len(client2.calls),
            )

    def test_run_steps_records_review_usage_on_success_and_failure(self):
        """Combined workflows persist pre-review and review usage at their respective stage
        boundaries.
        """
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as d:
                txt = os.path.join(d, "novel.txt")
                write_sample_txt(txt)
                cfg = _config(os.path.join(d, "state"))
                cfg.pipeline.review_agent_loop = False
                base_store = require_file_storage(
                    Orchestrator(
                        cfg,
                        client=FakeClient(handler=routing_handler),
                    ).run(txt)
                )

                def handler(messages, tier, json_mode):
                    if "translation reviewer" in messages[0]["content"]:
                        if fail:
                            raise RuntimeError("review failed")
                        return _review_json(messages[-1]["content"], [])
                    return routing_handler(messages, tier, json_mode)

                client = MeteredFakeClient(handler=handler)
                orch = Orchestrator(cfg, client=client)
                client.usage.record(
                    "cheap",
                    UsageSample(
                        prompt_tokens=11,
                        completion_tokens=7,
                        total_tokens=18,
                        cache_miss_tokens=11,
                    ),
                    "PreReview",
                )

                if fail:
                    with pytest.raises(RuntimeError, match="review failed"):
                        orch.run_steps(txt, {"review", "assemble"}, out_format="txt")
                else:
                    result = orch.run_steps(txt, {"review", "assemble"}, out_format="txt")
                    self.assertIsNotNone(result["review_result"])
                    self.assertTrue(Path(result["output"]).is_file())

                usage = base_store.load_usage()
                self.assertIsNotNone(usage)
                assert usage is not None
                self.assertEqual(usage["by_stage"]["PreReview"]["calls"], 1)
                self.assertIn("review.scan", usage["by_stage"])
                usage_events = [
                    json.loads(line)
                    for line in Path(base_store.event_log_path)
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if json.loads(line).get("event") == "usage_summary"
                ]
                self.assertTrue(usage_events)
                self.assertIn("review.scan", json.dumps(usage_events, ensure_ascii=False))

    def test_non_review_run_does_not_report_a_new_review_directory(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)

            reviewed = orch.run_review(txt)
            assembled = orch.run_steps(txt, {"assemble"}, out_format="txt")

            self.assertIsNotNone(reviewed["review_dir"])
            self.assertIsNone(assembled["review_dir"])
            self.assertEqual(
                assembled["store"].load_latest_review_result()["review_id"],
                reviewed["review_result"]["review_id"],
            )

    def test_conflict_arbitration_changes_final_review_suggestions(self):
        """Final arbitration rewrites losing proposals while preserving complete round records."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.review_fix_loop = False
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            orch.run(txt)
            progress_events: list[tuple[int, int, str]] = []

            def fake_review_chapters(chapters, terms, *, on_chapter_finished=None, **kwargs):
                all_issues = []
                for chapter in chapters:
                    proposed = "绫小路" if chapter.index == 0 else "绫小路君"
                    issues = [
                        {
                            "index": 0,
                            "_chunk_id": f"ch{chapter.index}-chunk",
                            "type": "terminology",
                            "detail": "译名不统一",
                            "suggestion": f"统一为{proposed}",
                            "consistency": {
                                "kind": "term",
                                "subject_source": "綾小路",
                                "proposed_value": proposed,
                            },
                        }
                    ]
                    if on_chapter_finished is not None:
                        on_chapter_finished(chapter, issues)
                    all_issues.append(issues)
                return all_issues

            def fake_arbitrate(arbiter, conflict):
                issue_ids = [issue["issue_id"] for issue in conflict["issues"]]
                return {
                    "conflict_id": conflict["conflict_id"],
                    "consistency_key": conflict["consistency_key"],
                    "issue_ids": issue_ids,
                    "status": "suggested",
                    "recommended_value": "绫小路",
                    "reason": "沿用首次译名。",
                    "supported_issue_ids": [issue_ids[0]],
                    "rejected_issue_ids": [issue_ids[1]],
                    "evidence_refs": [],
                }

            with (
                patch.object(
                    orch._review._chunks, "review_chapters", side_effect=fake_review_chapters
                ),
                patch(
                    "wenyi_core.pipeline.review.review_rounds.ReviewConflictArbiter.arbitrate",
                    new=fake_arbitrate,
                ),
            ):
                result = orch.run_review(
                    txt,
                    progress=lambda done, total, label, payload=None: progress_events.append(
                        (done, total, label)
                    ),
                )

            with open(
                os.path.join(
                    result["review_dir"],
                    "rounds/final/pre_arbitration_issues.json",
                ),
                encoding="utf-8",
            ) as file:
                before = json.load(file)
            final = result["review_result"]["issues"]
            internal_final = self._load_internal_issues(result)
            with open(
                os.path.join(
                    result["review_dir"],
                    "rounds/final/arbitration_superseded_issues.json",
                ),
                encoding="utf-8",
            ) as file:
                superseded = json.load(file)

            self.assertEqual(len(before), 2)
            self.assertEqual(len(final), 2)
            self.assertEqual(len(superseded), 1)
            self.assertEqual(
                {issue["consistency"]["proposed_value"] for issue in internal_final},
                {"绫小路"},
            )
            self.assertEqual(result["review_issues"], final)
            self.assertEqual(
                [
                    (done, total)
                    for done, total, label in progress_events
                    if label == "progress.stage.arbitration:1"
                ],
                [(0, 1), (1, 1)],
            )

    def test_shadow_fix_is_blindly_rereviewed_with_translation_context(self):
        """Temporary revisions enter the next review without modifying formal state files."""
        review_users: list[str] = []
        fix_users: list[str] = []

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                review_users.append(user)
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "语义不完整",
                            "suggestion": "补全原文信息",
                        },
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "人物译名不统一",
                            "suggestion": "沿用术语表译名",
                        },
                    ]
                    if len(review_users) == 1
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                fix_users.append(user)
                return _fix_json(user, "影子修订译文。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            client = MeteredFakeClient(handler=handler)
            orch = Orchestrator(cfg, client=client)
            store = require_file_storage(orch.run(txt))
            manifest_before = Path(store.manifest_path).read_bytes()
            chapter_before = Path(store.chapter_path(0)).read_bytes()

            progress_events: list[tuple[int, int, str]] = []
            result = orch.run_review(
                txt,
                progress=lambda done, total, label, payload=None: progress_events.append(
                    (done, total, label)
                ),
            )

            summary = result["review_result"]["summary"]
            patches = json.loads(
                Path(result["review_dir"], "rounds/final/patch-history.json").read_text(
                    encoding="utf-8"
                )
            )
            not_rereported = json.loads(
                Path(
                    result["review_dir"],
                    "rounds/final/not_rereported_patches.json",
                ).read_text(encoding="utf-8")
            )
            fixer_trace_exists = Path(
                result["review_dir"],
                "rounds/001/fixers/ch0-text1.json",
            ).is_file()
            manifest_after = Path(store.manifest_path).read_bytes()
            chapter_after = Path(store.chapter_path(0)).read_bytes()

        self.assertEqual(manifest_after, manifest_before)
        self.assertEqual(chapter_after, chapter_before)
        self.assertEqual(len(fix_users), 1)
        self.assertIn("语义不完整", fix_users[0])
        self.assertIn("人物译名不统一", fix_users[0])
        self.assertIn("Style guide: 克制", fix_users[0])
        self.assertIn("全书概览", fix_users[0])
        self.assertIn("本章梗概", fix_users[0])
        self.assertTrue(
            any("影子修订译文。" in user for user in review_users[2:]),
            "第二轮基础 Reviewer 必须直接读取影子译文",
        )
        self.assertEqual(result["review_issues"], [])
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")
        self.assertEqual(summary["review_round_count"], 3)
        self.assertEqual(summary["fix_round_count"], 1)
        self.assertEqual(summary["not_rereported_patch_count"], 1)
        self.assertEqual(len(patches), 1)
        self.assertEqual(len(patches[0]["issue_ids"]), 2)
        self.assertEqual(patches[0]["status"], "not_rereported")
        self.assertEqual(patches[0]["not_rereported_in_round"], 2)
        self.assertEqual(not_rereported, patches)
        self.assertFalse(Path(result["review_dir"], "verified_patches.json").exists())
        self.assertEqual(
            result["review_changes"],
            [
                {
                    "chapter": 0,
                    "index": 1,
                    "suggested_target": "影子修订译文。",
                    "issue_keys": patches[0]["issue_keys"],
                    "review_result": "not_rereported",
                }
            ],
        )
        self.assertTrue(fixer_trace_exists)
        self.assertEqual(
            [
                (done, total)
                for done, total, label in progress_events
                if label == "progress.stage.shadowRevision:1"
            ],
            [(0, 1), (1, 1)],
        )
        self.assertIn("progress.stage.reviewRound:2", [label for _, _, label in progress_events])

    def test_clean_first_pass_requires_an_independent_confirmation(self):
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            if "translation reviewer" in system:
                review_calls += 1
                return _review_json(messages[-1]["content"], [])
            if "cautious revision editor" in system:
                fix_calls += 1
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 4)  # Two chapters across two blind whole-book review rounds.
        self.assertEqual(fix_calls, 0)
        self.assertEqual(summary["review_round_count"], 2)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")

    def test_last_allowed_fix_still_gets_two_clean_review_passes(self):
        """Allow two complete blind review rounds after the final fix round."""
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": f"第 {call // 2 + 1} 轮仍需调整",
                            "suggestion": "继续改写",
                        }
                    ]
                    if call in {0, 2}
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                fix_calls += 1
                return _fix_json(user, f"影子版本 {fix_calls}。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 8)  # Two chapters across four whole-book review rounds.
        self.assertEqual(fix_calls, 2)
        self.assertEqual(summary["review_round_count"], 4)
        self.assertEqual(summary["fix_round_count"], 2)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")

    def test_clean_pass_before_a_fix_does_not_consume_post_fix_confirmation(self):
        """Clean rounds before a fix must not replace two independent confirmations after the
        patch.
        """
        review_calls = 0
        fix_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "第二轮才发现的问题",
                            "suggestion": "修订该段",
                        }
                    ]
                    if call == 2
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                fix_calls += 1
                return _fix_json(user, "迟发现问题的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 1
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 8)  # clean → issue/fix → clean → clean
        self.assertEqual(fix_calls, 1)
        self.assertEqual(summary["review_round_count"], 4)
        self.assertEqual(summary["clean_streak"], 2)
        self.assertEqual(result["review_result"]["termination"], "clean_confirmed")

    def test_failed_fixer_issue_survives_when_other_patch_passes_review(self):
        """Fixer failures remain unresolved even if the next reviewer omits them."""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "第一段需修订",
                            "suggestion": "修订第一段",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "第二段需修订",
                            "suggestion": "修订第二段",
                        },
                    ]
                    if call == 0
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "ch0:text0:" in user:
                    return _fix_json(user, "第一段影子修订。")
                return ""
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]
            patches = json.loads(
                Path(result["review_dir"], "rounds/final/patch-history.json").read_text(
                    encoding="utf-8"
                )
            )
            failures = json.loads(
                Path(result["review_dir"], "rounds/final/fix_failures.json").read_text(
                    encoding="utf-8"
                )
            )
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result["review_result"]["termination"], "unresolved_fixes")
        self.assertEqual(summary["blocked_issue_count"], 1)
        self.assertEqual(summary["issue_count"], 1)
        self.assertEqual(len(result["review_issues"]), 1)
        self.assertEqual(result["review_issues"][0]["index"], 1)
        self.assertEqual(internal_issues[0]["fix_failure"]["reason"], "malformed_json")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["status"], "not_rereported")
        self.assertEqual(len(failures), 1)

    def test_blocked_issue_survives_different_patch_on_same_segment(self):
        """A new patch must not clear historical fixer failures that it does not address."""
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                if call == 0:
                    issues = [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "用于推动循环的第一段问题",
                            "suggestion": "先修订第一段",
                        },
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "旧术语问题",
                            "suggestion": "沿用既有译名",
                        },
                    ]
                elif call == 2:
                    issues = [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "同段后来发现的新误译问题",
                            "suggestion": "补全该段语义",
                        }
                    ]
                else:
                    issues = []
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "旧术语问题" in user:
                    return ""
                if "用于推动循环的第一段问题" in user:
                    return _fix_json(user, "第一段影子修订。")
                if "同段后来发现的新误译问题" in user:
                    return _fix_json(user, "第二段针对新误译的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_tokens_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(review_calls, 6)  # Three whole-book review rounds, each with two chapters.
        self.assertEqual(result["review_result"]["termination"], "unresolved_fixes")
        self.assertEqual(summary["blocked_issue_count"], 1)
        self.assertEqual(len(result["review_issues"]), 1)
        issue = result["review_issues"][0]
        self.assertEqual((issue["chapter"], issue["index"]), (0, 1))
        self.assertEqual(issue["type"], "terminology")
        self.assertEqual(issue["detail"], "旧术语问题")
        internal_issue = next(
            item for item in internal_issues if item["issue_key"] == issue["issue_key"]
        )
        self.assertEqual(internal_issue["fix_failure"]["reason"], "malformed_json")

    def test_same_segment_same_type_fix_failures_remain_distinct(self):
        """Two independent issues of the same type in one paragraph must not overwrite each
        other's blocked state.
        """
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            if "translation reviewer" in system:
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "人物动作漏译",
                            "suggestion": "补回人物动作",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "时间关系误译",
                            "suggestion": "修正时间关系",
                        },
                    ]
                    if review_calls == 1
                    else []
                )
                return _review_json(messages[-1]["content"], issues)
            if "cautious revision editor" in system:
                return ""
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_tokens_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result["review_result"]["termination"], "no_progress")
        self.assertEqual(summary["blocked_issue_count"], 2)
        self.assertEqual(
            {issue["detail"] for issue in result["review_issues"]},
            {"人物动作漏译", "时间关系误译"},
        )
        self.assertTrue(
            all(issue["fix_failure"]["reason"] == "malformed_json" for issue in internal_issues)
        )

    def test_rereported_blocked_issue_is_deduplicated_across_rounds(self):
        """Repeated logical issues use the latest evidence while retaining prior fixer-failure
        information.
        """
        review_calls = 0

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                issues = []
                if call == 0:
                    issues.append(
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "用于进入下一轮的问题",
                            "suggestion": "先修订第一段",
                        }
                    )
                if call in {0, 2}:
                    issues.append(
                        {
                            "index": 1,
                            "type": "terminology",
                            "detail": "跨轮重复的术语问题",
                            "suggestion": "统一使用既有译名",
                        }
                    )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "跨轮重复的术语问题" in user:
                    return ""
                if "用于进入下一轮的问题" in user:
                    return _fix_json(user, "用于进入下一轮的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_tokens_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 1
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            result = orch.run_review(txt)
            internal_issues = self._load_internal_issues(result)

        repeated = [
            issue
            for issue in internal_issues
            if (
                issue.get("chapter"),
                issue.get("index"),
                issue.get("type"),
                issue.get("detail"),
            )
            == (0, 1, "terminology", "跨轮重复的术语问题")
        ]
        self.assertEqual(result["review_result"]["termination"], "max_rounds")
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["review_round"], 2)
        self.assertEqual(repeated[0]["fix_failure"]["reason"], "malformed_json")
        self.assertEqual(repeated[0]["fix_failure"]["review_round"], 1)

    def test_rejected_cycle_patch_does_not_clear_prior_blocked_issue(self):
        """A rejected cyclic overlay must not clear historical blocked issues even when its
        patch applies.
        """
        review_calls = 0
        original_targets: dict[int, str] = {}

        def handler(messages, tier, json_mode):
            nonlocal review_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                call = review_calls
                review_calls += 1
                if call == 0:
                    issues = [
                        {
                            "index": 0,
                            "type": "terminology",
                            "detail": "循环前已阻塞的术语问题",
                            "suggestion": "使用既有译名",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "先生成版本 B",
                            "suggestion": "改写第二段",
                        },
                    ]
                elif call == 2:
                    issues = [
                        {
                            "index": 0,
                            "type": "mistranslation",
                            "detail": "同段新发现的问题",
                            "suggestion": "保持第一段原译",
                        },
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "把第二段恢复原译",
                            "suggestion": "恢复第二段",
                        },
                    ]
                else:
                    issues = []
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                if "循环前已阻塞的术语问题" in user:
                    return ""
                if "先生成版本 B" in user:
                    return _fix_json(user, "影子版本 B。")
                if "同段新发现的问题" in user:
                    return _fix_json(user, original_targets[0])
                if "把第二段恢复原译" in user:
                    return _fix_json(user, original_targets[1])
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.segment.max_tokens_per_batch = 100_000
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            store = require_file_storage(orch.run(txt))
            chapter = store.load_chapter(0)
            original_targets = {
                0: chapter.text_segments[0].target or "",
                1: chapter.text_segments[1].target or "",
            }

            result = orch.run_review(txt)
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result["review_result"]["termination"], "cycle_detected")
        blocked = [
            issue for issue in internal_issues if issue.get("detail") == "循环前已阻塞的术语问题"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["fix_failure"]["reason"], "malformed_json")

    def test_final_summary_includes_blocked_conflicts_and_fallbacks(self):
        """Final summaries must rebuild from all unresolved issues, not only the final clean
        round.
        """

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "cautious revision editor" in system:
                return _fix_json(user, "用于进入盲审轮的影子修订。")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = True
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_clean_confirmations = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)

            def fake_review_chapters(
                chapters,
                terms,
                *,
                review_round,
                on_chapter_finished=None,
                **kwargs,
            ):
                all_issues = []
                for chapter in chapters:
                    if review_round != 1:
                        issues = []
                    elif chapter.index == 0:
                        issues = [
                            {
                                "index": 0,
                                "_chunk_id": "conflict-a",
                                "type": "terminology",
                                "detail": "第一种译名",
                                "suggestion": "统一为绫小路",
                                "consistency": {
                                    "kind": "term",
                                    "subject_source": "綾小路",
                                    "proposed_value": "绫小路",
                                },
                            },
                            {
                                "index": 1,
                                "_chunk_id": "fallback-a",
                                "type": "mistranslation",
                                "detail": "Agent 未完成核验",
                                "suggestion": "人工复核",
                                "agent_fallback": True,
                                "fallback_reason": "max_rounds",
                            },
                            {
                                "index": 2,
                                "_chunk_id": "fixable-a",
                                "type": "mistranslation",
                                "detail": "用于进入下一轮的可修问题",
                                "suggestion": "修订该段",
                            },
                        ]
                    else:
                        issues = [
                            {
                                "index": 0,
                                "_chunk_id": "conflict-b",
                                "type": "terminology",
                                "detail": "第二种译名",
                                "suggestion": "统一为绫小路君",
                                "consistency": {
                                    "kind": "term",
                                    "subject_source": "綾小路",
                                    "proposed_value": "绫小路君",
                                },
                            }
                        ]
                    if on_chapter_finished is not None:
                        on_chapter_finished(chapter, issues)
                    all_issues.append(issues)
                return all_issues

            with patch.object(
                orch._review._chunks, "review_chapters", side_effect=fake_review_chapters
            ):
                result = orch.run_review(txt)

            review_dir = Path(result["review_dir"])
            result_json = json.loads((review_dir / "result.json").read_text(encoding="utf-8"))
            summary = result_json["summary"]
            conflicts = json.loads(
                (review_dir / "rounds/final/conflicts.json").read_text(encoding="utf-8")
            )
            residual = json.loads(
                (review_dir / "rounds/final/residual_conflicts.json").read_text(encoding="utf-8")
            )
            internal_issues = self._load_internal_issues(result)

        self.assertEqual(result_json["termination"], "unresolved_fixes")
        self.assertEqual(summary["issue_count"], 3)
        self.assertEqual(summary["blocked_issue_count"], 3)
        self.assertEqual(summary["conflict_count"], 1)
        self.assertEqual(summary["unresolved_conflict_count"], 1)
        self.assertEqual(summary["fallback_agent_count"], 1)
        self.assertEqual(result_json["summary"], summary)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(len(residual), 1)
        unresolved_ids = {issue["issue_id"] for issue in internal_issues}
        conflict_ids = set(conflicts[0]["issue_ids"])
        residual_ids = set(residual[0]["issue_ids"])
        self.assertEqual(conflict_ids, residual_ids)
        self.assertTrue(conflict_ids <= unresolved_ids)

    def test_shadow_loop_detects_a_b_a_oscillation(self):
        review_calls = 0
        fix_calls = 0
        original_target = ""

        def handler(messages, tier, json_mode):
            nonlocal review_calls, fix_calls
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "translation reviewer" in system:
                current = review_calls
                review_calls += 1
                issues = (
                    [
                        {
                            "index": 1,
                            "type": "mistranslation",
                            "detail": "仍需调整",
                            "suggestion": "改写",
                        }
                    ]
                    if current % 2 == 0
                    else []
                )
                return _review_json(user, issues)
            if "cautious revision editor" in system:
                replacement = "影子版本 B。" if fix_calls == 0 else original_target
                fix_calls += 1
                return _fix_json(user, replacement)
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as directory:
            txt = os.path.join(directory, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(directory, "state"))
            cfg.pipeline.review_agent_loop = False
            cfg.pipeline.review_conflict_arbitration = False
            cfg.pipeline.review_fix_loop = True
            cfg.pipeline.review_fix_max_rounds = 2
            cfg.pipeline.review_concurrency = 1
            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            store = require_file_storage(orch.run(txt))
            original_target = store.load_chapter(0).text_segments[1].target or ""

            result = orch.run_review(txt)
            summary = result["review_result"]["summary"]

        self.assertEqual(review_calls, 4)  # Two chapters across two rounds; no third round runs.
        self.assertEqual(fix_calls, 2)
        self.assertEqual(summary["review_round_count"], 2)
        self.assertEqual(result["review_result"]["termination"], "cycle_detected")
