"""EPUB 布局分析的流水线调度、缓存与刷新回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.config import Config
from trans_novel.epub.layout import (
    LayoutInventory,
    LayoutProfile,
)
from trans_novel.epub.layout import (
    LayoutNode as LayoutDataNode,
)
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application
from trans_novel.pipeline.contracts import NodeRequest
from trans_novel.pipeline.execution import RequiredNodeFailed
from trans_novel.pipeline.nodes.layout import LayoutNode, current_layout_state
from trans_novel.pipeline.state import NODE_SUCCEEDED, NodeState, RunState, RunStore


def _config(root: Path, *, models=("p",)) -> Config:
    config = Config.from_dict(
        {
            "llm": fake_llm_dict(models=models),
            "quality": "economy",
            "output": {
                "bilingual": {"enabled": False},
                "override_theme": {"styles": "builtin:chinese-reading"},
            },
        }
    )
    config.source_lang = "en"
    config.target_lang = "zh"
    config.state_dir = str(root / "state")
    return config


def _source(root: Path) -> str:
    path = root / "book.txt"
    path.write_text(
        "A complete source paragraph for layout-aware translation.\n\n"
        "Another complete paragraph provides stable structural evidence.",
        encoding="utf-8",
    )

    return str(path)


class TestLayoutPipeline(unittest.TestCase):
    def test_layout_fingerprint_invalidates_only_output(self):
        state = RunState(
            nodes={
                "layout": NodeState(
                    node_id="layout",
                    status=NODE_SUCCEEDED,
                    input_fingerprint="old-layout",
                ),
                "translate:0": NodeState(
                    node_id="translate:0",
                    status=NODE_SUCCEEDED,
                    input_fingerprint="paid-translation",
                ),
                "assemble": NodeState(
                    node_id="assemble",
                    status=NODE_SUCCEEDED,
                    input_fingerprint="old-output",
                ),
            }
        )

        invalidated = state.reconcile_fingerprints({"layout": "new-layout"})

        self.assertEqual(invalidated, {"layout", "assemble"})
        self.assertEqual(state.nodes["translate:0"].status, NODE_SUCCEEDED)
        self.assertEqual(
            state.nodes["translate:0"].input_fingerprint,
            "paid-translation",
        )

    def test_cached_profile_must_cover_exact_inventory_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            store.save_state(RunState())
            inventory = LayoutInventory(
                source_sha256="a" * 64,
                nodes=(
                    LayoutDataNode(
                        node_id="node",
                        resource_href="chapter.xhtml",
                        path=(0,),
                        source_sha256="c" * 64,
                        group_key="group",
                        sample={},
                    ),
                ),
                digest="b" * 64,
                policy_version="1",
            )
            forged = LayoutProfile(
                source_sha256=inventory.source_sha256,
                inventory_digest=inventory.digest,
                policy_version=inventory.policy_version,
                assignments=(),
                provenance={},
            )
            store.write_json(store.layout_profile_path, forged.to_dict())

            with patch(
                "trans_novel.pipeline.nodes.layout.build_layout_inventory",
                return_value=inventory,
            ):
                actual_inventory, profile = current_layout_state(store, "book.epub")

            self.assertIs(actual_inventory, inventory)
            self.assertIsNone(profile)

    def test_interrupted_refresh_resumes_but_completed_refresh_starts_new_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory)
            inventory = LayoutInventory(
                source_sha256="a" * 64,
                nodes=(),
                digest="b" * 64,
                policy_version="1",
            )
            profile = LayoutProfile(
                source_sha256=inventory.source_sha256,
                inventory_digest=inventory.digest,
                policy_version=inventory.policy_version,
                assignments=(),
                provenance={},
            )
            store.write_json(store.layout_profile_path, profile.to_dict())
            request = NodeRequest(
                store=store,
                node_id="layout",
                key="layout",
                ci=None,
                scope="book",
                input_path="book.epub",
                shared=SimpleNamespace(layout_profile=None),
            )
            node = LayoutNode(
                analyzer=object(),
                config=_config(Path(directory)),
                inventory=inventory,
                profile=profile,
                refresh=True,
            )
            saved = {"schema_version": 1, "observations": {"node": {"role": "body"}}}

            def interrupt(_inventory, _analyzer, *, checkpoint, save_checkpoint):
                self.assertIsNone(checkpoint)
                save_checkpoint(saved)
                raise RuntimeError("interrupted")

            with (
                patch("trans_novel.pipeline.nodes.layout.analyze_layout", side_effect=interrupt),
                self.assertRaisesRegex(RuntimeError, "interrupted"),
            ):
                node.execute(request)
            self.assertEqual(store.load_layout_profile(), profile.to_dict())

            def resume(_inventory, _analyzer, *, checkpoint, save_checkpoint):
                self.assertEqual(checkpoint, saved)
                return profile

            with patch("trans_novel.pipeline.nodes.layout.analyze_layout", side_effect=resume):
                outcome = node.execute(request)

            self.assertEqual(outcome.fingerprint, profile.digest)
            saved_profile = store.load_layout_profile()
            self.assertEqual(
                saved_profile["provenance"]["analyst_configuration"],
                {"candidates": list(node.config.llm.models.analyst)},
            )
            self.assertEqual(LayoutProfile.from_dict(saved_profile).digest, profile.digest)
            accepted_attempt_id = saved_profile["provenance"]["analysis_attempt_id"]
            self.assertEqual(
                store.load_layout_work()["attempt_id"],
                accepted_attempt_id,
            )

            accepted_profile = LayoutProfile.from_dict(saved_profile)
            repeated = LayoutNode(
                analyzer=object(),
                config=node.config,
                inventory=inventory,
                profile=accepted_profile,
                refresh=True,
            )

            def repeat(_inventory, _analyzer, *, checkpoint, save_checkpoint):
                self.assertIsNone(checkpoint)
                return profile

            with patch("trans_novel.pipeline.nodes.layout.analyze_layout", side_effect=repeat):
                repeated.execute(request)
            repeated_attempt_id = store.load_layout_profile()["provenance"]["analysis_attempt_id"]
            self.assertNotEqual(repeated_attempt_id, accepted_attempt_id)

    def test_full_run_analyzes_once_and_completed_run_is_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _source(root)
            notices: list[str] = []

            def first_handler(messages, agent, operation, json_mode):
                if operation == "layout.classify":
                    self.assertTrue(any("模型" in notice for notice in notices))
                return routing_handler(messages, agent, operation, json_mode)

            first_client = FakeClient(handler=first_handler)
            first = Application(_config(root), client=first_client).run_all(
                source,
                out_path=str(root / "first.epub"),
                progress=lambda _done, _total, message: notices.append(message),
            )

            self.assertIn("layout.classify", {call["operation"] for call in first_client.calls})
            self.assertTrue(Path(first["store"].layout_profile_path).is_file())
            targets = [
                segment.target
                for chapter in first["store"].load_state().chapters
                for segment in first["store"].load_chapter(chapter.index).text_segments
            ]

            cached_notices: list[str] = []
            offline = FakeClient(handler=lambda *_args: self.fail("缓存运行不得调用模型"))
            second = Application(_config(root), client=offline).run_all(
                source,
                out_path=str(root / "second.epub"),
                progress=lambda _done, _total, message: cached_notices.append(message),
            )

            self.assertEqual(offline.calls, [])
            self.assertTrue(any("复用" in notice for notice in cached_notices))
            events = Path(first["store"].event_log_path).read_text(encoding="utf-8")
            self.assertIn('"event": "layout_analysis_started"', events)
            self.assertIn('"event": "layout_profile_reused"', events)
            self.assertEqual(
                [
                    segment.target
                    for chapter in second["store"].load_state().chapters
                    for segment in second["store"].load_chapter(chapter.index).text_segments
                ],
                targets,
            )

    def test_model_change_reuses_profile_and_failed_refresh_preserves_paid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _source(root)
            first = Application(_config(root), client=FakeClient(handler=routing_handler)).run_all(
                source, out_path=str(root / "first.epub")
            )
            store = first["store"]
            profile_before = Path(store.layout_profile_path).read_bytes()
            targets_before = [
                segment.target
                for chapter in store.load_state().chapters
                for segment in store.load_chapter(chapter.index).text_segments
            ]

            changed_model = FakeClient(handler=lambda *_args: self.fail("有效 profile 不得重跑"))
            Application(_config(root, models=("x", "y", "z")), client=changed_model).assemble(
                store,
                source,
                out_path=str(root / "model-change.epub"),
            )
            self.assertEqual(changed_model.calls, [])

            def fail_refresh(*_args):
                raise RuntimeError("layout provider unavailable")

            with self.assertRaises(RequiredNodeFailed):
                Application(_config(root), client=FakeClient(handler=fail_refresh)).assemble(
                    store,
                    source,
                    out_path=str(root / "refresh.epub"),
                    reanalyze_layout=True,
                )

            self.assertEqual(Path(store.layout_profile_path).read_bytes(), profile_before)
            self.assertEqual(
                [
                    segment.target
                    for chapter in store.load_state().chapters
                    for segment in store.load_chapter(chapter.index).text_segments
                ],
                targets_before,
            )


if __name__ == "__main__":
    unittest.main()
