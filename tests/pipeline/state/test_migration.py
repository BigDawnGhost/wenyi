"""V1/V2 to V3 state migration regressions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from trans_novel.pipeline import build_workflow_definition
from trans_novel.pipeline.contracts import GOAL_TRANSLATE
from trans_novel.pipeline.planning import (
    Planner,
    PrescanInputs,
    WorkflowPolicy,
    frozen_input_fingerprint,
)
from trans_novel.pipeline.state import (
    NODE_PENDING,
    RUN_STATE_SCHEMA_VERSION,
    ChapterStatus,
    IdentityMismatchError,
    NodeStatus,
    RunStore,
)
from trans_novel.pipeline.state.frozen import FrozenBookPreparation, FrozenPreparationMap


def _write_v2(root: str, source: str) -> bytes:
    os.makedirs(os.path.join(root, "chapters_v2"), exist_ok=True)
    payload = {
        "index": 0,
        "title": "Chapter",
        "template": None,
        "segments": [
            {
                "index": 0,
                "source": "Source.",
                "target": "已完成译文。",
                "kind": "text",
                "cont": False,
                "meta": {},
            },
            {
                "index": 1,
                "source": "Interior.",
                "target": "内部译文。",
                "kind": "text",
                "cont": False,
                "meta": {},
            },
        ],
        "meta": {
            "source_digest": "obsolete",
            "naturalized": True,
            "backtranslation_issues": [{"detail": "obsolete"}],
        },
    }
    with open(os.path.join(root, "chapters_v2", "ch0.json"), "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    with open(source, "rb") as source_stream:
        source_bytes_sha256 = hashlib.sha256(source_stream.read()).hexdigest()
    identity = {
        "source_bytes_sha256": source_bytes_sha256,
        "run_input_schema_version": 1,
        "source_lang": "en",
        "target_lang": "zh",
    }
    manifest = {
        "run_state_schema": 2,
        "identity": identity,
        "title": "Book",
        "fmt": "text",
        "source_path": source,
        "source_lang": "en",
        "target_lang": "zh",
        "initialized": True,
        "chapters": [{"index": 0, "title": "Chapter", "status": "done"}],
        "progress": {
            "0": {
                "status": "done",
                "pending_polish": [{"start": 0, "count": 1}],
                "review_issues": [
                    {"stage": "lint", "index": 1, "type": "too_short", "detail": "short"},
                    {"stage": "length", "index": 0, "type": "too_long", "detail": "long"},
                    {"stage": "model", "index": 0, "type": "meaning", "detail": "discard"},
                ],
                "back_matter_mode": "light",
            }
        },
        "nodes": {
            "translate:0": {
                "node_id": "translate:0",
                "status": "succeeded",
                "input_fingerprint": "old",
            },
            "naturalize:0": {"node_id": "naturalize:0", "status": "failed_permanent"},
        },
        "analysis_flags": {"term_mining_done": True},
    }
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False)
    return payload["segments"][1]["target"].encode("utf-8")


class TestV3Migration(unittest.TestCase):
    def test_state_facade_exports_statuses_and_frozen_fingerprint(self):
        self.assertEqual(NODE_PENDING, "pending")
        self.assertEqual(ChapterStatus.__args__, ("pending", "done"))
        self.assertIn("succeeded", NodeStatus.__args__)
        book = FrozenBookPreparation(
            book_id="book",
            source_sha256="source",
            analysis={},
            style="",
            style_brief="",
            glossary=(),
            node_fingerprints={},
        )
        frozen = FrozenPreparationMap("preparation", {"book": book})
        self.assertEqual(
            frozen.node_fingerprint(
                book=book,
                node_id="translate",
                source_mapping=("book", 0),
                content="source",
            ),
            frozen_input_fingerprint("preparation", "translate", ("book", 0), "source"),
        )

    def test_direct_v2_to_v3_preserves_targets_and_pipeline_progress(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "book.txt")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write("Source.\nInterior.")
            root = os.path.join(d, "run")
            expected = _write_v2(root, source)
            store = RunStore(root)
            state = store.load_state()
            self.assertEqual(state.run_state_schema, RUN_STATE_SCHEMA_VERSION)
            self.assertEqual(RunStore(root).load_chapter(0).segments[1].target.encode(), expected)
            self.assertEqual(state.progress[0].pending_polish[0].count, 1)
            self.assertEqual(len(state.progress[0].lint_issues), 2)
            self.assertEqual(
                {issue["stage"] for issue in state.progress[0].lint_issues}, {"lint", "length"}
            )
            self.assertEqual(state.progress[0].back_matter_mode, "light")
            self.assertFalse(any("naturalize" in key for key in state.nodes))
            self.assertEqual(state.nodes["translate:0"].input_fingerprint, "")
            with self.assertRaises(IdentityMismatchError):
                Planner(build_workflow_definition()).build_plan(
                    goal=GOAL_TRANSLATE,
                    store=store,
                    policy=WorkflowPolicy(),
                    prescan=PrescanInputs(),
                )
            self.assertEqual(RunStore(root).load_chapter(0).segments[1].target.encode(), expected)

    def test_v1_routes_through_v2_and_reaches_v3(self):
        with tempfile.TemporaryDirectory() as d:
            source = os.path.join(d, "book.txt")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write("Source.")
            root = os.path.join(d, "run")
            os.makedirs(os.path.join(root, "chapters"), exist_ok=True)
            chapter = {
                "index": 0,
                "title": "Chapter",
                "template": None,
                "segments": [
                    {
                        "index": 0,
                        "source": "Source.",
                        "target": "译文。",
                        "kind": "text",
                        "cont": False,
                        "meta": {},
                    }
                ],
                "meta": {"pending_polish": [{"start": 0, "count": 1}], "review_issues": []},
            }
            with open(os.path.join(root, "chapters", "ch0.json"), "w", encoding="utf-8") as stream:
                json.dump(chapter, stream, ensure_ascii=False)
            manifest = {
                "title": "Book",
                "fmt": "text",
                "source_path": source,
                "source_lang": "en",
                "target_lang": "zh",
                "initialized": True,
                "chapters": [{"index": 0, "title": "Chapter", "status": "done"}],
            }
            with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as stream:
                json.dump(manifest, stream)
            state = RunStore(root).load_state()
            self.assertEqual(state.run_state_schema, RUN_STATE_SCHEMA_VERSION)
            self.assertEqual(RunStore(root).load_chapter(0).segments[0].target, "译文。")
