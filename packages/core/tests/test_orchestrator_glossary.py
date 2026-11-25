"""Offline end-to-end orchestration and resume tests using FakeClient."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest

from wenyi_core.glossary.store import GlossaryStore
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.run_store import STATUS_PENDING

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import fake_pipeline_config as _config
from tests.pipeline_fixtures import require_file_storage
from tests.review_fixtures import review_json as _review_json
from tests.sample_data import write_sample_txt


class TestFullGlossary(unittest.TestCase):
    def _run_with_terms(self, d):
        from wenyi_core.glossary.store import GlossaryTerm

        txt = os.path.join(d, "novel.txt")
        write_sample_txt(txt)
        cfg = _config(os.path.join(d, "state"))

        orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
        store = require_file_storage(orch.prepare(txt))
        g = GlossaryStore(store.glossary_path)
        # Include an absent character, an unrelated term and an entity whose alias occurs in the chapter.
        g.upsert_term(GlossaryTerm(source="外部人物X", target="外部译名", type="person"))
        g.upsert_term(GlossaryTerm(source="無関係用語", target="无关术语", type="term"))
        g.upsert_term(
            GlossaryTerm(source="ホリキタ", target="堀北译名", aliases=["堀北"], type="term")
        )
        g.close()

        client = FakeClient(handler=routing_handler)
        Orchestrator(cfg, client=client).run(txt)
        return [
            "\n".join(m["content"] for m in c["messages"])
            for c in client.calls
            if "literary translator" in c["messages"][0]["content"]
        ]

    def test_every_translation_batch_keeps_absent_terms_and_aliases(self):
        with tempfile.TemporaryDirectory() as d:
            translate_prompts = self._run_with_terms(d)
            self.assertTrue(translate_prompts)
            for p in translate_prompts:
                self.assertIn("外部人物X", p)
                self.assertIn("無関係用語", p)
                self.assertIn("ホリキタ", p)

    def test_batch_glossary_refreshes_following_prompts(self):
        """Extract terms after each batch so later prompts receive new forms of address.

        The extraction overlaps the next batch's translation, so its entries reach the
        batch after that.
        """

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "literary translator" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
                return json.dumps(
                    {"translations": ["小夏帆" for _ in range(n)]}, ensure_ascii=False
                )
            if (
                "terminology" in system
                and "extractor" in system
                and "夏帆ちゃん" in user
                and "小夏帆" in user
            ):
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "夏帆ちゃん",
                                "target": "小夏帆",
                                "type": "appellation",
                                "scope": "stable",
                                "aliases": ["夏帆"],
                                "note": "亲昵称呼",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n「夏帆ちゃん」と母親が言った。\n\n夏帆ちゃんは窓の外を見た。\n\n"
                    "夏帆ちゃんは笑った。\n"
                )
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.review = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_tokens_per_batch = 10

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(txt)

            translate_prompts = [
                "\n".join(m["content"] for m in c["messages"])
                for c in client.calls
                if "literary translator" in c["messages"][0]["content"]
            ]
            body = [
                prompt
                for prompt in translate_prompts
                if "[0] " in prompt and "夏帆" in prompt.split("[0] ", 1)[1].split("\n", 1)[0]
            ]
            self.assertEqual(len(body), 3)
            self.assertNotIn("夏帆ちゃん → 小夏帆", body[1])
            self.assertIn("夏帆ちゃん → 小夏帆", body[2])

    def test_resume_recovers_batch_glossary_checkpoints_from_events(self):
        """Reuse extraction events for old state without duplicate model calls for completed
        batches.
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
                Orchestrator(cfg, client=FakeClient(handler=routing_handler)).run(
                    txt, only_chapter=0
                )
            )
            checkpoints = store.completed_batch_glossary_keys(0)
            self.assertGreater(len(checkpoints), 1)

            # Reset a completed chapter to pending; resume must recognize extracted batches from events.
            store.set_chapter_status(0, STATUS_PENDING)

            labels: list[str] = []
            glossary_labels: list[str] = []

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                if "terminology" in system and "extractor" in system:
                    glossary_labels.append(labels[-1])
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            Orchestrator(cfg, client=client).run(
                txt,
                only_chapter=0,
                progress=lambda _done, _total, label: labels.append(label),
            )

            glossary_calls = [
                call
                for call in client.calls
                if "terminology" in call["messages"][0]["content"]
                and "extractor" in call["messages"][0]["content"]
            ]
            # Skip all saved batches and retain only the chapter-end fallback extraction.
            self.assertEqual(len(glossary_calls), 1)
            self.assertTrue(glossary_labels)
            self.assertNotIn("progress.stage.parsing", glossary_labels)

    def test_final_glossary_is_available_to_review_prompt(self):
        """Terms discovered in later chapters must be available to final review from the first
        chapter onward.
        """

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "literary translator" in system:
                n = len(re.findall(r"^\[(\d+)\]", user, re.MULTILINE))
                return json.dumps(
                    {"translations": ["小夏帆" for _ in range(n)]}, ensure_ascii=False
                )
            if "terminology" in system and "extractor" in system and "後半で" in user:
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "夏帆ちゃん",
                                "target": "小夏帆",
                                "type": "appellation",
                                "scope": "stable",
                                "aliases": ["夏帆"],
                                "note": "亲昵称呼",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if "terminology" in system and "extractor" in system:
                return json.dumps({"terms": []}, ensure_ascii=False)
            if "terminology consistency aligner" in system:
                self.assertIn("「夏帆ちゃん」と母親が言った。", user)
                self.assertIn('"target": "小夏帆"', user)
                return json.dumps(
                    {"terms": [{"source": "夏帆ちゃん", "target": "小夏帆"}]},
                    ensure_ascii=False,
                )
            if "translation reviewer" in system:
                self.assertIn("夏帆ちゃん → 小夏帆", system)
                return _review_json(user, [])
            return routing_handler(messages, tier, json_mode)

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "# 第一章\n\n「夏帆ちゃん」と母親が言った。\n\n"
                    "# 第二章\n\n後半で夏帆ちゃんが再び現れた。\n"
                )
            cfg = _config(os.path.join(d, "state"))
            cfg.pipeline.polish = False
            cfg.pipeline.book_understanding = False
            cfg.segment.max_tokens_per_batch = 200

            orch = Orchestrator(cfg, client=FakeClient(handler=handler))
            orch.run(txt)
            orch.run_review(txt)
