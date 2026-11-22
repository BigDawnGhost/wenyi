"""Offline end-to-end orchestration and resume tests using FakeClient."""

from __future__ import annotations

import os
import tempfile
import unittest

import pytest
from wenyi_core.glossary.store import GlossaryStore
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator

from tests.fake_llm import MeteredFakeClient, routing_handler
from tests.pipeline_fixtures import fake_pipeline_config as _config
from tests.pipeline_fixtures import require_file_storage
from tests.sample_data import write_sample_txt


class TestBookUnderstanding(unittest.TestCase):
    def test_book_synopsis_failure_does_not_block_translation_or_digest_reuse(self):
        def failing_synopsis(messages, tier, json_mode):
            if "whole-book synopsis writer" in messages[0]["content"]:
                raise TimeoutError("synopsis provider unavailable")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=failing_synopsis)
            store = require_file_storage(Orchestrator(cfg, client=client).run(txt))
            self.assertFalse((store.load_analysis() or {}).get("book_synopsis"))
            for item in store.load_manifest()["chapters"]:
                chapter = store.load_chapter(item["index"])
                self.assertTrue(chapter.meta["source_digest"])
                self.assertTrue(
                    all(segment.target is not None for segment in chapter.text_segments)
                )
            user = self._translate_user(client.calls)
            self.assertIn("[Whole-book synopsis]\n(none)", user)
            self.assertIn("本章梗概", user)

            resumed = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=resumed).run(txt)
            analysis = store.load_analysis()
            assert analysis is not None
            self.assertTrue(analysis["book_synopsis"])
            self.assertFalse(any(call["operation"] == "synopsis.chapter" for call in resumed.calls))
            self.assertFalse(any(call["operation"] == "translation.body" for call in resumed.calls))

    def test_required_digest_failure_saves_usage_and_resumes_without_recounting(self):
        def failing_digest(messages, tier, json_mode):
            if (
                "chapter digest writer" in messages[0]["content"]
                and "放課後" in messages[-1]["content"]
            ):
                raise TimeoutError("chapter digest provider unavailable")
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = MeteredFakeClient(handler=failing_digest)
            orch = Orchestrator(cfg, client=client)
            cfg.pipeline.book_understanding = False
            store = require_file_storage(orch.prepare(txt))
            cfg.pipeline.book_understanding = True
            cfg.freeze_language_policies(store.load_manifest()["source_sha256"])
            with pytest.raises(ValueError, match="Chapter digests.*1"):
                orch.run(txt)
            self.assertTrue(store.load_chapter(0).meta["source_digest"])
            self.assertFalse(store.load_chapter(1).meta.get("source_digest"))
            self.assertFalse(any(call["operation"] == "translation.body" for call in client.calls))
            usage = store.load_usage()
            assert usage is not None
            self.assertEqual(usage["totals"], client.usage_summary()["totals"])

            client.handler = routing_handler
            previous_calls = len(client.calls)
            orch.run(txt)
            digests = [
                call
                for call in client.calls[previous_calls:]
                if call["operation"] == "synopsis.chapter"
            ]
            self.assertEqual(len(digests), 1)
            self.assertIn("放課後", digests[0]["messages"][-1]["content"])
            usage = store.load_usage()
            assert usage is not None
            self.assertEqual(usage["totals"], client.usage_summary()["totals"])

    def _translate_user(self, calls) -> str:
        """Return user text from the last translation.body call (not a polish continuation)."""
        for c in reversed(calls):
            if c.get("operation") == "translation.body":
                return c["messages"][-1]["content"]
            if "literary translator" in c["messages"][0]["content"] and (
                "Polish the translations from your previous JSON response"
                not in c["messages"][-1]["content"]
            ):
                return c["messages"][-1]["content"]
        return ""

    def test_prepass_builds_and_injects(self):
        """Prescan produces and injects chapter digests and the whole-book synopsis."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))

            client = FakeClient(handler=routing_handler)
            store = require_file_storage(Orchestrator(cfg, client=client).run(txt))

            # Persist chapter digests in chapter.meta.
            self.assertTrue(store.load_chapter(0).meta.get("source_digest"))
            # Persist the whole-book synopsis in analysis.
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))

            # Translation prompts contain actual synopsis/digest content instead of empty placeholders.
            user = self._translate_user(client.calls)
            self.assertIn("[Whole-book synopsis]", user)
            self.assertIn("[Chapter digest]", user)
            self.assertIn("全书概览", user)  # Fake whole-book synopsis text.
            self.assertIn("本章梗概", user)  # Fake chapter digest text.

    def test_prepare_for_translation_builds_understanding_without_targets(self):
        """Preparation persists analysis and synopsis without extracting terms or translating the
        body.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            client = FakeClient(handler=routing_handler)

            store = require_file_storage(
                Orchestrator(
                    cfg,
                    client=client,
                ).prepare_for_translation(txt)
            )

            manifest = store.load_manifest()
            self.assertTrue(store.load_analysis())
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))
            glossary = GlossaryStore(store.glossary_path)
            try:
                self.assertEqual(glossary.stats()["terms"], 0)
            finally:
                glossary.close()
            for item in manifest["chapters"]:
                chapter = store.load_chapter(item["index"])
                self.assertTrue(chapter.meta.get("source_digest"))
                self.assertTrue(all(segment.target is None for segment in chapter.segments))
            translate_calls = [
                call
                for call in client.calls
                if "literary translator" in call["messages"][0]["content"]
            ]
            self.assertEqual(translate_calls, [])

    def test_prescan_parallel(self):
        """Parallel digests are persisted in chapter order and injected into translation
        correctly.
        """
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.prescan_concurrency = 3

            client = FakeClient(handler=routing_handler)
            store = require_file_storage(Orchestrator(cfg, client=client).run(txt))

            m = store.load_manifest()
            for c in m["chapters"]:
                self.assertTrue(store.load_chapter(c["index"]).meta.get("source_digest"))
            self.assertTrue((store.load_analysis() or {}).get("book_synopsis"))
            user = self._translate_user(client.calls)
            self.assertIn("[Chapter digest]", user)

    def test_resume_skips_prepass(self):
        """Resume reuses saved digests and synopsis without new prescan calls."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            Orchestrator(cfg, client=FakeClient(handler=routing_handler)).run(txt)

            c2 = FakeClient(handler=routing_handler)
            Orchestrator(cfg, client=c2).run(txt)
            prepass = [
                c
                for c in c2.calls
                if "梗概员" in c["messages"][0]["content"]
                or "概览员" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(prepass), 0)

    def test_toggle_off(self):
        """Disabling book_understanding skips prescan and uses empty prompt placeholders."""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.book_understanding = False

            client = FakeClient(handler=routing_handler)
            store = require_file_storage(Orchestrator(cfg, client=client).run(txt))

            self.assertFalse(store.load_chapter(0).meta.get("source_digest"))
            self.assertFalse((store.load_analysis() or {}).get("book_synopsis"))
            prepass = [
                c
                for c in client.calls
                if "梗概员" in c["messages"][0]["content"]
                or "概览员" in c["messages"][0]["content"]
            ]
            self.assertEqual(len(prepass), 0)
