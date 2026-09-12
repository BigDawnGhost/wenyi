"""润色拒绝证据与提交顺序的离线回归。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.polisher import Polisher
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from trans_novel.ingest.models import Chapter, ChapterProcessing, Document, Segment
from trans_novel.llm import FakeClient
from trans_novel.llm.errors import ProviderError
from trans_novel.pipeline.contracts import NodeRequest
from trans_novel.pipeline.nodes.polish import PolishNode
from trans_novel.pipeline.state import PolishBatch, RunIdentity, RunStore


def _config() -> Config:
    config = Config.from_dict({"llm": fake_llm_dict(), "quality": "quality"})
    config.source_lang = "en"
    config.pipeline.protocol_retry_limit = 0
    config.pipeline.inflight_glossary = False
    config.punctuation_normalize = False
    return config


def _stage(store: RunStore, chapter: Chapter) -> None:
    state = store.stage_document(
        Document(
            title="Book",
            fmt="text",
            source_lang="en",
            target_lang="zh",
            chapters=[chapter],
        ),
        RunIdentity(source_bytes_sha256="source", source_lang="en", target_lang="zh"),
    )
    store.save_manifest(state)


def _request(store: RunStore, executor, futures=None) -> NodeRequest:
    return NodeRequest(
        store=store,
        node_id="polish",
        key="polish:0",
        ci=0,
        scope="chapter",
        input_path="",
        executor=executor,
        shared=SimpleNamespace(polish_futures=futures or {}),
        total_chapters=1,
    )


def _node(client, glossary, config) -> PolishNode:
    return PolishNode(
        polisher=Polisher(client, config),
        extractor=None,
        glossary=glossary,
        config=config,
        style_brief="",
    )


class TestPolishAudit(unittest.TestCase):
    def test_partial_batch_records_complete_proposal_and_protocol_evidence(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            config = _config()
            glossary = GlossaryStore(f"{directory}/glossary.db")
            self.addCleanup(glossary.close)
            segments = [
                Segment(
                    index=7,
                    source='"Hello," she said.',
                    target="“你好，”她说。",
                    resource_href="text/chapter.xhtml",
                    anchor="p7",
                ),
                Segment(index=11, source="World news.", target="世界新闻。"),
            ]
            store = RunStore(directory)
            _stage(store, Chapter(index=0, title="Chapter", segments=segments))
            progress = store.load_progress(0)
            progress.pending_polish = [PolishBatch(start=0, count=2)]
            store.save_progress(0, progress)
            client = FakeClient(
                handler=lambda *_: json.dumps(
                    {"polished": [{"id": 0, "text": " 你好，她说。 "}]},
                    ensure_ascii=False,
                )
            )

            _node(client, glossary, config).execute(_request(store, executor))

            rows = [
                json.loads(line)
                for line in Path(store.event_log_path).read_text(encoding="utf-8").splitlines()
            ]
            rejected = [row for row in rows if row["event"] == "polish_rejected"]
            self.assertEqual([row["index"] for row in rejected], [7, 11])
            proposal, protocol = rejected
            self.assertEqual(proposal["proposal"], " 你好，她说。 ")
            self.assertEqual(proposal["checked_proposal"], "你好，她说。")
            self.assertEqual(proposal["batch_start"], 0)
            self.assertEqual(proposal["batch_offset"], 0)
            self.assertEqual(proposal["resource_href"], "text/chapter.xhtml")
            self.assertEqual(proposal["anchor"], "p7")
            self.assertEqual(proposal["reasons"], ["quote_loss"])
            self.assertEqual(proposal["proposal_issues"][0]["type"], "quote_loss")
            self.assertIsNone(protocol["proposal"])
            self.assertIsNone(protocol["checked_proposal"])
            self.assertEqual(protocol["proposal_issues"], [])
            self.assertEqual(protocol["reasons"], ["polish_item_missing"])
            mandatory = {
                "audit_id",
                "chapter",
                "index",
                "batch_start",
                "batch_offset",
                "resource_href",
                "anchor",
                "source",
                "pre_polish_target",
                "proposal",
                "checked_raw",
                "checked_proposal",
                "raw_issues",
                "proposal_issues",
                "locked_terms",
                "reasons",
                "strategy",
                "configured_editor_models",
                "source_sha256",
                "pre_polish_target_sha256",
                "proposal_sha256",
            }
            self.assertTrue(mandatory <= proposal.keys())
            self.assertEqual(store.load_progress(0).pending_polish, [])

    def test_provider_fallback_uses_request_snapshot_after_glossary_changes(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            config = _config()
            glossary = GlossaryStore(f"{directory}/glossary.db")
            self.addCleanup(glossary.close)
            glossary.upsert_term(
                GlossaryTerm(source="Alice", target="爱丽丝", confidence="high", locked=True)
            )
            segment = Segment(index=3, source="Alice arrived.", target="爱丽丝来了。")
            store = RunStore(directory)
            _stage(store, Chapter(index=0, title="Chapter", segments=[segment]))
            progress = store.load_progress(0)
            progress.pending_polish = [PolishBatch(start=0, count=1)]
            store.save_progress(0, progress)
            started = threading.Event()
            release = threading.Event()

            def respond(*_):
                started.set()
                release.wait(timeout=2)
                raise ProviderError("provider failed")

            client = FakeClient(handler=respond)
            polisher = Polisher(client, config)
            request_terms = tuple(glossary.all_terms())
            future = executor.submit(
                polisher.polish,
                [segment.target],
                [segment.source],
                glossary_terms=request_terms,
                strict=True,
            )
            self.assertTrue(started.wait(timeout=2))
            glossary.lock_term("Alice", "艾丽斯")
            release.set()

            _node(client, glossary, config).execute(
                _request(store, executor, {(0, 0): (future, request_terms)})
            )
            rows = [
                json.loads(line)
                for line in Path(store.event_log_path).read_text(encoding="utf-8").splitlines()
            ]
            rejected = next(row for row in rows if row["event"] == "polish_rejected")
            self.assertIsNone(rejected["proposal"])
            self.assertEqual(rejected["reasons"], ["ProviderError"])
            self.assertEqual(rejected["locked_terms"][0]["target"], "爱丽丝")
            self.assertEqual(store.load_chapter(0).segments[0].target, "爱丽丝来了。")

    def test_required_audit_failure_prevents_checkpoint_commit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            config = _config()
            glossary = GlossaryStore(f"{directory}/glossary.db")
            self.addCleanup(glossary.close)
            segment = Segment(index=4, source='"Hello," she said.', target="“你好，”她说。")
            store = RunStore(directory)
            _stage(store, Chapter(index=0, title="Chapter", segments=[segment]))
            progress = store.load_progress(0)
            progress.pending_polish = [PolishBatch(start=0, count=1)]
            store.save_progress(0, progress)
            client = FakeClient(
                handler=lambda *_: json.dumps(
                    {"polished": [{"id": 0, "text": "你好，她说。"}]}, ensure_ascii=False
                )
            )

            with (
                mock.patch.object(
                    store, "log_event_required", side_effect=OSError("append failed")
                ),
                self.assertRaisesRegex(OSError, "append failed"),
            ):
                _node(client, glossary, config).execute(_request(store, executor))

            self.assertEqual(store.load_chapter(0).segments[0].target, "“你好，”她说。")
            self.assertEqual(store.load_progress(0).pending_polish, [PolishBatch(start=0, count=1)])
            self.assertIsNone(store.load_journal())

    def test_preserved_chapter_is_noop_and_pending_work_is_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config()
            glossary = GlossaryStore(f"{directory}/glossary.db")
            self.addCleanup(glossary.close)
            client = FakeClient(handler=lambda *_: self.fail("preserved chapter must not call LLM"))
            chapter = Chapter(index=0, title="References", segments=[Segment(index=0, source="A")])
            chapter.set_processing(
                ChapterProcessing(
                    action="preserve",
                    review_required=False,
                    reason="reference list",
                    source_sha256="source",
                    strategy_version="chapter_semantics_v1",
                )
            )
            store = RunStore(directory)
            _stage(store, chapter)
            request = _request(store, None)

            outcome = _node(client, glossary, config).execute(request)

            self.assertIsNone(outcome.fingerprint)
            self.assertEqual(client.calls, [])
            progress = store.load_progress(0)
            progress.pending_polish = [PolishBatch(start=0, count=1)]
            store.save_progress(0, progress)
            with self.assertRaisesRegex(ValueError, "preserved chapter has pending polish work"):
                _node(client, glossary, config).execute(request)


if __name__ == "__main__":
    unittest.main()
