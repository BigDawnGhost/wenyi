"""EPUB 布局分析的流水线调度、缓存与刷新回归。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from tests.fixtures.books import write_nested_toc_epub
from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.cli import app as cli_app
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
from trans_novel.pipeline.state import (
    NODE_FAILED_RETRYABLE,
    NODE_SUCCEEDED,
    NodeState,
    RunState,
    RunStore,
)
from trans_novel.pipeline.state.models import TRANSLATION_POLICY_VERSION


def _config(root: Path, *, models=("p",), theme: bool = True) -> Config:
    output = {"bilingual": {"enabled": False}}
    if theme:
        output["override_theme"] = {"styles": "builtin:chinese-reading"}
    config = Config.from_dict(
        {
            "llm": fake_llm_dict(models=models),
            "quality": "economy",
            "output": output,
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


def _completed_legacy_epub(root: Path) -> tuple[Path, RunStore]:
    source = root / "legacy.epub"
    write_nested_toc_epub(str(source))
    result = Application(
        _config(root, models=("old",), theme=False),
        client=FakeClient(handler=routing_handler),
    ).run_all(str(source), out_path=str(root / "initial.epub"))
    store = result["store"]
    manifest = store.load_manifest()
    manifest["identity"]["translation_policy_version"] = TRANSLATION_POLICY_VERSION - 1
    store.save_manifest(manifest)
    Path(store.layout_profile_path).unlink(missing_ok=True)
    return source, store


def _paid_state_snapshot(store: RunStore) -> dict:
    state = store.load_state()
    output_nodes = {"layout", "report", "assemble"}
    return {
        "identity": state.identity.model_dump(mode="json"),
        "chapters": tuple(
            Path(store.chapter_path(chapter.index)).read_bytes() for chapter in state.chapters
        ),
        "content_nodes": {
            key: (node.status, node.input_fingerprint)
            for key, node in sorted(state.nodes.items())
            if key.split(":", 1)[0] not in output_nodes
        },
    }


def _invoke_cli(
    config: Config,
    client: FakeClient,
    command: str,
    source: Path,
    *,
    out_format: str = "epub",
):
    def application_factory(_loaded_config):
        return Application(config, client=client)

    with patch("trans_novel.pipeline.Application", side_effect=application_factory):
        return CliRunner().invoke(
            cli_app,
            [command, str(source), "--format", out_format],
        )


def _mark_output_failed(store: RunStore) -> None:
    state = store.load_state()
    for node_id in ("layout", "report", "assemble"):
        state.nodes[node_id] = state.nodes[node_id].model_copy(
            update={"status": NODE_FAILED_RETRYABLE}
        )
    store.save_state(state)


def _legacy_txt(root: Path) -> tuple[Path, RunStore]:
    source = Path(_source(root))
    result = Application(
        _config(root, theme=False),
        client=FakeClient(handler=routing_handler),
    ).run_all(str(source), out_format="txt", out_path=str(root / "initial.txt"))
    store = result["store"]
    manifest = store.load_manifest()
    manifest["identity"]["translation_policy_version"] = TRANSLATION_POLICY_VERSION - 1
    store.save_manifest(manifest)
    return source, store


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


class TestCompletedLegacyCli(unittest.TestCase):
    def test_translate_adds_layout_then_translate_and_resume_reuse_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, store = _completed_legacy_epub(root)
            before = _paid_state_snapshot(store)
            config = _config(root, models=("translator", "new-analyst", "editor"))

            client = FakeClient(handler=routing_handler)
            result = _invoke_cli(config, client, "translate", source)

            self.assertEqual(result.exit_code, 0, result.output)
            operations = [call["operation"] for call in client.calls]
            self.assertTrue(operations)
            self.assertEqual(set(operations), {"layout.classify"})
            profile = store.load_layout_profile()
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(
                profile["provenance"]["analyst_configuration"],
                {"candidates": list(config.llm.models.analyst)},
            )
            verification = store.load_epub_verification()
            self.assertIsNotNone(verification)
            assert verification is not None
            self.assertTrue(verification["passed"])
            self.assertGreater(verification["theme"]["resources"], 0)
            self.assertEqual(_paid_state_snapshot(store), before)

            for command in ("translate", "resume"):
                with self.subTest(command=command):
                    cached = FakeClient(
                        handler=lambda *_args: self.fail("有效布局的普通续跑不得调用任何模型")
                    )
                    repeated = _invoke_cli(config, cached, command, source)
                    self.assertEqual(repeated.exit_code, 0, repeated.output)
                    self.assertEqual(cached.calls, [])
                    self.assertEqual(_paid_state_snapshot(store), before)

    def test_failed_output_nodes_retry_missing_and_stale_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, store = _completed_legacy_epub(root)
            before = _paid_state_snapshot(store)
            config = _config(root, models=("translator", "retry-analyst", "editor"))
            _mark_output_failed(store)

            missing_client = FakeClient(handler=routing_handler)
            missing = _invoke_cli(config, missing_client, "translate", source)

            self.assertEqual(missing.exit_code, 0, missing.output)
            self.assertEqual(
                {call["operation"] for call in missing_client.calls},
                {"layout.classify"},
            )
            self.assertEqual(_paid_state_snapshot(store), before)
            stale = store.load_layout_profile()
            self.assertIsNotNone(stale)
            assert stale is not None
            stale["inventory_digest"] = "0" * 64
            store.write_json(store.layout_profile_path, stale)
            Path(store.layout_work_path).unlink(missing_ok=True)
            _mark_output_failed(store)

            stale_client = FakeClient(handler=routing_handler)
            retried = _invoke_cli(config, stale_client, "resume", source)

            self.assertEqual(retried.exit_code, 0, retried.output)
            self.assertEqual(
                {call["operation"] for call in stale_client.calls},
                {"layout.classify"},
            )
            self.assertNotEqual(
                store.load_layout_profile()["inventory_digest"],
                "0" * 64,
            )
            self.assertEqual(_paid_state_snapshot(store), before)

    def test_incomplete_future_and_source_mismatch_refuse_before_model_work(self):
        cases = ("incomplete", "future", "source_mismatch")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, store = _completed_legacy_epub(root)
                if case == "incomplete":
                    chapter = store.load_chapter(0)
                    chapter.text_segments[0].reset_translation()
                    store.save_chapter(chapter)
                elif case == "future":
                    manifest = store.load_manifest()
                    manifest["identity"]["translation_policy_version"] = (
                        TRANSLATION_POLICY_VERSION + 1
                    )
                    store.save_manifest(manifest)
                else:
                    with source.open("ab") as stream:
                        stream.write(b"source identity changed")
                before = _paid_state_snapshot(store)
                client = FakeClient(handler=lambda *_args: self.fail("拒绝路径不得调用模型"))

                result = _invoke_cli(_config(root), client, "translate", source)

                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(client.calls, [])
                self.assertEqual(_paid_state_snapshot(store), before)

    def test_legacy_txt_ignores_unused_theme_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, store = _legacy_txt(root)
            before = _paid_state_snapshot(store)
            config = Config.from_dict(
                {
                    "llm": fake_llm_dict(models=("changed",)),
                    "quality": "economy",
                    "output": {
                        "mono": True,
                        "bilingual": {"enabled": False},
                        "override_theme": {"styles": str(root / "missing-general.css")},
                        "bilingual_styles": str(root / "missing-bilingual.css"),
                    },
                },
                base_dir=root,
            )
            config.source_lang = "en"
            config.target_lang = "zh"
            config.state_dir = str(root / "state")
            client = FakeClient(handler=lambda *_args: self.fail("TXT 遗留输出不得调用模型"))

            result = _invoke_cli(config, client, "translate", source, out_format="txt")

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(client.calls, [])
            self.assertEqual(_paid_state_snapshot(store), before)


if __name__ == "__main__":
    unittest.main()
