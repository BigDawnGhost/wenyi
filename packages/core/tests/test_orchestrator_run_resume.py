"""Offline end-to-end orchestration and resume tests using FakeClient."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.usage import UsageSample
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.translation import TranslationService
from wenyi_core.storage.chapter_publication import JOURNAL_NAME
from wenyi_core.storage.run_store import STATUS_DONE, STATUS_PENDING

from tests.fake_llm import MeteredFakeClient, routing_handler
from tests.pipeline_fixtures import fake_pipeline_config as _config
from tests.pipeline_fixtures import require_file_storage
from tests.sample_data import write_sample_txt


def test_resume_finalized_chapter_after_manifest_disk_failure(tmp_path, monkeypatch):
    source = tmp_path / "novel.txt"
    source.write_text("綾小路は教室の窓際に座っていた。", encoding="utf-8")
    cfg = _config(str(tmp_path / "state"))
    # Keep synopsis invalidation after glossary growth out of this checkpoint test.
    cfg.pipeline.book_understanding = False
    client_args = {
        "handler": routing_handler,
        "usage_sample": UsageSample(prompt_tokens=7, completion_tokens=3, total_tokens=10),
    }
    first_client = MeteredFakeClient(**client_args)
    first = Orchestrator(cfg, client=first_client)
    store = require_file_storage(first.prepare(str(source)))
    journal = Path(store.run_dir) / JOURNAL_NAME
    real_replace = os.replace

    def fail_manifest_after_chapter_commit(temp, target):
        if os.path.basename(target) == "manifest.json" and journal.is_file():
            raise OSError("manifest device full")
        real_replace(temp, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_manifest_after_chapter_commit)
        with pytest.raises(OSError, match="manifest device full"):
            first.run(str(source), only_chapter=0)

    assert journal.is_file()
    assert store.load_manifest()["chapters"][0]["status"] == STATUS_DONE
    saved_translation = store.load_chapter(0)
    usage = store.load_usage()
    assert usage is not None, "Completed body calls must already be in the durable ledger"
    assert usage["by_stage"]["translation.body"]["total_tokens"] > 0
    second_client = MeteredFakeClient(**client_args)
    second = Orchestrator(cfg, client=second_client)
    resumed = second.run(str(source))
    # Title translation had not started before the disk failure. It is new work;
    # recovery must not request or charge for completed body, polish or glossary calls.
    assert [call["operation"] for call in second_client.calls] == ["translation.title"]
    assert resumed.load_chapter(0) == saved_translation
    resumed_usage = resumed.load_usage()
    assert resumed_usage is not None, "Recovery must preserve the usage ledger"
    assert resumed_usage["by_stage"] == {
        **usage["by_stage"],
        "translation.title": second_client.usage_summary()["by_stage"]["translation.title"],
    }
    assert resumed_usage["totals"]["total_tokens"] == usage["totals"]["total_tokens"] + 10
    assert not journal.exists()


def _translated_para_count(calls) -> int:
    """Count source paragraphs sent to translation by their numbered lines."""
    n = 0
    for c in calls:
        if "literary translator" in c["messages"][0]["content"]:
            n += len(re.findall(r"^\[(\d+)\]", c["messages"][-1]["content"], re.MULTILINE))
    return n


class TestOrchestratorRunResume(unittest.TestCase):
    def test_prepare_retries_after_analysis_failure(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            def fail_analysis(messages, tier, json_mode):
                if "pre-translation analyst" in messages[0]["content"]:
                    raise RuntimeError("temporary model failure")
                return routing_handler(messages, tier, json_mode)

            with pytest.raises(RuntimeError, match="temporary model failure"):
                Orchestrator(cfg, client=FakeClient(handler=fail_analysis)).prepare(txt)

            run_dirs = [
                os.path.join(cfg.state_dir, name, "targets", "zh")
                for name in os.listdir(cfg.state_dir)
            ]
            self.assertEqual(len(run_dirs), 1)
            self.assertFalse(os.path.isfile(os.path.join(run_dirs[0], "manifest.json")))

            store = require_file_storage(
                Orchestrator(cfg, client=FakeClient(handler=routing_handler)).prepare(txt)
            )
            self.assertTrue(store.exists())
            self.assertTrue(store.load_manifest()["initialized"])
            self.assertIsNotNone(store.load_analysis())

    def test_full_run_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            store = require_file_storage(orch.run(txt))

            # Every chapter is marked done.
            m = store.load_manifest()
            self.assertEqual(len(m["chapters"]), 2)
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m["chapters"]))

            # Every paragraph has a polished translation.
            ch0 = store.load_chapter(0)
            self.assertTrue(all(s.target for s in ch0.text_segments))
            self.assertTrue(
                all((s.target_before_polish or "").startswith("译") for s in ch0.text_segments)
            )
            self.assertTrue(all((s.target or "").startswith("润") for s in ch0.text_segments))

            # Persist both pre-polish and polished text in chapter JSON, not just in runtime models.
            with open(store.chapter_path(0), encoding="utf-8") as chapter_file:
                chapter_json = json.load(chapter_file)
            self.assertTrue(
                all(
                    "target_before_polish" in segment
                    for segment in chapter_json["segments"]
                    if segment["source"].strip()
                )
            )

            # Only translation-time extraction contributes glossary entries.
            from wenyi_core.glossary.store import GlossaryStore

            g = GlossaryStore(store.glossary_path)
            self.assertIsNone(g.get_term("綾小路"))
            self.assertIsNotNone(g.get_term("堀北"))
            g.close()

            # Resume completed chapters without further translation calls.
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            orch2.run(txt)  # Resume semantics.
            translate_calls = [
                c for c in client2.calls if "literary translator" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(translate_calls), 0)

    def test_resume_after_partial(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = _config(state)

            client = FakeClient(handler=routing_handler)
            orch = Orchestrator(cfg, client=client)
            # Translate chapter zero only.
            store = require_file_storage(orch.run(txt, only_chapter=0))
            m = store.load_manifest()
            self.assertEqual(m["chapters"][0]["status"], STATUS_DONE)
            self.assertNotEqual(m["chapters"][1]["status"], STATUS_DONE)

            # Resume should translate only chapter one.
            client2 = FakeClient(handler=routing_handler)
            orch2 = Orchestrator(cfg, client=client2)
            chapter_indices = [chapter["index"] for chapter in m["chapters"]]
            expected_total, expected_done = orch2._translation.progress_counts(
                store, chapter_indices
            )
            progress_events: list[tuple[int, int, str]] = []
            store2 = orch2.run(
                txt,
                progress=lambda done, total, label, payload=None: progress_events.append(
                    (done, total, label)
                ),
            )
            m2 = store2.load_manifest()
            self.assertTrue(all(c["status"] == STATUS_DONE for c in m2["chapters"]))
            chapter_label = TranslationService.chapter_progress_label(
                store.load_chapter(1).title, 1
            )
            first_chapter_progress = next(
                event for event in progress_events if event[2] == chapter_label
            )
            self.assertEqual(
                first_chapter_progress,
                (expected_done, expected_total, chapter_label),
            )


class TestSegmentLevelResume(unittest.TestCase):
    def _tr_handler(self, tag):
        """Return a tagged translation handler and delegate other tasks to the shared router."""

        def handler(messages, tier, json_mode):
            if "literary translator" in messages[0]["content"]:
                n = len(re.findall(r"^\[(\d+)\]", messages[-1]["content"], re.MULTILINE))
                return json.dumps(
                    {"translations": [f"{tag}译{i}" for i in range(n)]},
                    ensure_ascii=False,
                )
            return routing_handler(messages, tier, json_mode)

        return handler

    def test_resume_skips_done_segments_keeps_their_text(self):
        """Resume preserves completed targets and translates only unfinished paragraphs."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_tokens_per_batch = (
                8  # Use roughly one paragraph per batch for precise resume assertions.
            )
            cfg.pipeline.polish = False  # Preserve translation tags for assertions; this setting is unrelated to resume behavior.

            # First run: translate chapter zero with the R1 tag.
            c1 = FakeClient(handler=self._tr_handler("R1"))
            store = require_file_storage(Orchestrator(cfg, client=c1).run(txt, only_chapter=0))
            ch = store.load_chapter(0)
            self.assertTrue(all(s.target and s.target.startswith("R1") for s in ch.text_segments))

            # Simulate interruption by unsetting the last target (None = pending; "" would count as done).
            ch.segments[-1].target = None
            store.save_chapter(ch)
            store.set_chapter_status(0, STATUS_PENDING)

            # Resume with R2 and translate only the single cleared paragraph.
            c2 = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=c2).run(txt, only_chapter=0)
            self.assertEqual(
                _translated_para_count(c2.calls), 1
            )  # Only one paragraph is translated again.

            ch2 = store.load_chapter(0)
            # Saved paragraphs retain R1 without cross-position reuse; the resumed paragraph gets R2.
            first_target = ch2.text_segments[0].target
            last_target = ch2.text_segments[-1].target
            self.assertIsNotNone(first_target)
            self.assertIsNotNone(last_target)
            assert first_target is not None
            assert last_target is not None
            self.assertTrue(first_target.startswith("R1"))
            self.assertTrue(last_target.startswith("R2"))

    def test_resume_splits_mixed_batch_after_budget_change(self):
        """One missing target in a large batch must not overwrite its saved neighboring
        targets.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.segment.max_tokens_per_batch = 100_000
            cfg.pipeline.polish = False

            first_client = FakeClient(handler=self._tr_handler("R1"))
            store = require_file_storage(
                Orchestrator(cfg, client=first_client).run(txt, only_chapter=0)
            )
            chapter = store.load_chapter(0)
            chapter.text_segments[-1].target = None
            store.save_chapter(chapter)
            store.set_chapter_status(0, STATUS_PENDING)

            # Changing the budget can still group completed and unset targets together.
            cfg.segment.max_tokens_per_batch = 50_000
            second_client = FakeClient(handler=self._tr_handler("R2"))
            Orchestrator(cfg, client=second_client).run(txt, only_chapter=0)

            self.assertEqual(_translated_para_count(second_client.calls), 1)
            resumed = store.load_chapter(0).text_segments
            self.assertTrue(
                all((segment.target or "").startswith("R1") for segment in resumed[:-1])
            )
            self.assertTrue((resumed[-1].target or "").startswith("R2"))

    def test_resume_skips_do_not_refresh_term_snapshot_each_batch(self):
        """Pure resume skips avoid glossary refreshes; missing checkpoints extract and refresh
        before translation.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_tokens_per_batch = 8

            store = require_file_storage(
                Orchestrator(cfg, client=FakeClient(handler=self._tr_handler("R1"))).run(
                    txt, only_chapter=0
                )
            )
            chapter = store.load_chapter(0)
            segments = chapter.text_segments
            self.assertGreater(len(segments), 2)
            # Leave the last paragraph pending (None) and remove the first extraction checkpoint.
            segments[-1].target = None
            store.save_chapter(chapter)
            store.set_chapter_status(0, STATUS_PENDING)
            first_key = store.batch_glossary_key(0, 1)
            self.assertIn(first_key, store.completed_batch_glossary_keys(0))
            kept_lines: list[str] = []
            for line in Path(store.event_log_path).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if (
                    event.get("event") == "batch_glossary_extracted"
                    and event.get("chapter") == 0
                    and event.get("start_index") == 0
                    and event.get("count") == 1
                ):
                    continue
                kept_lines.append(line)
            Path(store.event_log_path).write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
            store._batch_glossary_event_cache = None
            self.assertNotIn(first_key, store.completed_batch_glossary_keys(0))

            extract_batch_calls = {"n": 0}
            orch = Orchestrator(
                cfg, client=FakeClient(handler=self._tr_handler("R2")), storage=store
            )
            real_extract = orch._translation.extract_batch_glossary

            def counting_extract(*args, **kwargs):
                extract_batch_calls["n"] += 1
                return real_extract(*args, **kwargs)

            with (
                patch.object(store, "all_terms", wraps=store.all_terms) as snapshots,
                # Isolate translation snapshot reads from extraction's own glossary reads.
                patch.object(orch._runtime.extractor, "extract_and_store", return_value={}),
                patch.object(
                    orch._translation,
                    "extract_batch_glossary",
                    side_effect=counting_extract,
                ),
            ):
                orch.run(txt, only_chapter=0)

            # Extract once for the missing checkpoint and once after the final translation; chapter fallback is separate.
            self.assertEqual(extract_batch_calls["n"], 2)
            # Read at chapter start and refresh once before real translation; checkpointed skips do not refresh.
            self.assertEqual(snapshots.call_count, 2)
            resumed = store.load_chapter(0).text_segments
            self.assertTrue((resumed[-1].target or "").startswith("R2"))
            self.assertTrue(
                all((segment.target or "").startswith("R1") for segment in resumed[:-1])
            )
