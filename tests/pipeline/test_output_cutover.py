"""持久化有效输出与流水线切换回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from tests.fixtures.fake_llm import fake_llm_dict, routing_handler
from trans_novel.assemble.epub.rendering.theme import (
    ThemeError,
    resolve_theme,
    semantic_output_digest,
)
from trans_novel.config import Config, OutputConfig
from trans_novel.llm import FakeClient
from trans_novel.pipeline import Application
from trans_novel.pipeline.composition.output import resolve_effective_output
from trans_novel.pipeline.execution.runner import RequiredNodeFailed
from trans_novel.pipeline.state import (
    IdentityMismatchError,
    SavedOutputSelection,
    load_output_selection,
)

_SOURCE_SHA = "a" * 64


def _source(directory: str) -> str:
    path = Path(directory, "book.txt")
    path.write_text("A complete source paragraph for translation.", encoding="utf-8")
    return str(path)


def _config(directory: str, *, output: dict | None = None, source_lang: str = "en") -> Config:
    raw = {"llm": fake_llm_dict()}
    if output is not None:
        raw["output"] = output
    config = Config.from_dict(raw)
    config.source_lang = source_lang
    config.target_lang = "zh"
    config.state_dir = str(Path(directory, "state"))
    return config


def _saved(selection: dict, origins: dict[str, str] | None = None) -> SavedOutputSelection:
    return SavedOutputSelection(
        source_bytes_sha256=_SOURCE_SHA,
        selection=selection,
        origins=origins or {},
    )


def _write_v1_selection(store) -> None:
    store.write_json(
        str(Path(store.run_dir, "output_selection.json")),
        {
            "schema_version": 1,
            "source_bytes_sha256": store.load_state().identity.source_bytes_sha256,
            "selection": {
                "mono": True,
                "bilingual": {"enabled": False, "order": "target_first"},
                "override_theme": {
                    "rules": "builtin:legacy-javascript",
                    "styles": "builtin:chinese-reading",
                },
                "bilingual_styles": "builtin:bilingual",
            },
            "origins": {"override_theme.rules": "legacy.yaml"},
        },
    )


class TestEffectiveOutputResolution(unittest.TestCase):
    def test_explicit_values_override_saved_and_explicit_null_clears_theme(self):
        saved = _saved(
            {
                "mono": False,
                "bilingual": {"enabled": True, "order": "source_first"},
                "override_theme": {"styles": "/saved/theme.css"},
                "bilingual_styles": "/saved/bilingual.css",
            },
            {
                "override_theme.styles": "saved.yaml",
                "bilingual_styles": "saved.yaml",
            },
        )
        current = OutputConfig.model_validate({"mono": True, "override_theme": None})

        output, origins, normalized = resolve_effective_output(
            current, {}, saved, out_format="epub"
        )

        self.assertTrue(output.mono)
        self.assertTrue(output.bilingual.enabled)
        self.assertEqual(output.bilingual.order, "source_first")
        self.assertIsNone(output.override_theme)
        self.assertEqual(output.bilingual_styles, "/saved/bilingual.css")
        self.assertEqual(origins, {"bilingual_styles": "saved.yaml"})
        self.assertFalse(normalized)

    def test_txt_retains_saved_latent_theme_and_strictly_rejects_malformed_selection(self):
        saved = _saved(
            {
                "mono": True,
                "bilingual": {"enabled": False, "order": "source_first"},
                "override_theme": {"styles": "/saved/theme.css"},
                "bilingual_styles": "/saved/bilingual.css",
            },
            {"override_theme.styles": "saved.yaml"},
        )
        current = OutputConfig.model_validate(
            {
                "override_theme": {"styles": "/current/theme.css"},
                "bilingual_styles": "/current/bilingual.css",
            }
        )

        output, origins, _ = resolve_effective_output(
            current,
            {
                "override_theme.styles": "current.yaml",
                "bilingual_styles": "current.yaml",
            },
            saved,
            out_format="txt",
        )

        assert output.override_theme is not None
        self.assertEqual(output.override_theme.styles, "/saved/theme.css")
        self.assertEqual(output.bilingual_styles, "/saved/bilingual.css")
        self.assertEqual(origins, {"override_theme.styles": "saved.yaml"})
        malformed = _saved({"mono": 1})
        with self.assertRaises(ValidationError):
            resolve_effective_output(OutputConfig(), {}, malformed, out_format="epub")


class TestPipelineOutputSetup(unittest.TestCase):
    def test_missing_theme_fails_before_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            css = Path(directory, "missing.css")
            config = _config(
                directory,
                output={
                    "bilingual": {"enabled": False},
                    "override_theme": {"styles": str(css)},
                },
            )
            client = FakeClient(handler=lambda *_args: self.fail("主题失败前不得调用模型"))

            with self.assertRaisesRegex(ThemeError, "asset_not_found"):
                Application(config, client=client).prepare(_source(directory))

            self.assertEqual(client.calls, [])
            self.assertEqual(list(Path(config.state_dir).glob("*/output_selection.json")), [])

    def test_selection_is_saved_before_first_paid_language_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory, source_lang="auto")

            def handler(messages, agent, operation, json_mode):
                records = list(Path(config.state_dir).glob("*/output_selection.json"))
                self.assertEqual(len(records), 1)
                return routing_handler(messages, agent, operation, json_mode)

            Application(config, client=FakeClient(handler=handler)).prepare(_source(directory))

    def test_txt_never_reads_missing_theme_and_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_css = str(Path(directory, "missing.css"))
            config = _config(
                directory,
                output={
                    "bilingual": {"enabled": False},
                    "override_theme": {"styles": missing_css},
                },
            )
            source = _source(directory)
            result = Application(config, client=FakeClient(handler=routing_handler)).run_all(
                source, out_format="txt"
            )
            self.assertIsInstance(result["output_digest"], str)
            saved = load_output_selection(
                result["store"],
                source_bytes_sha256=result["store"].load_state().identity.source_bytes_sha256,
            )
            assert saved is not None
            self.assertEqual(saved.selection["override_theme"]["styles"], missing_css)
            config_before = config.output.model_dump()
            fields_before = (
                set(config.output.model_fields_set),
                set(config.output.bilingual.model_fields_set),
            )

            offline = FakeClient(handler=lambda *_args: self.fail("TXT 重装不得调用模型"))
            outputs = Application(config, client=offline).assemble(
                result["store"],
                source,
                out_format="txt",
                out_path=str(Path(directory, "again.txt")),
                mono=False,
                bilingual=True,
            )
            self.assertEqual(len(outputs), 1)
            self.assertEqual(offline.calls, [])
            self.assertEqual(config.output.model_dump(), config_before)
            self.assertEqual(
                (
                    config.output.model_fields_set,
                    config.output.bilingual.model_fields_set,
                ),
                fields_before,
            )

    def test_theme_byte_edits_rebuild_without_model_calls_and_path_is_not_semantic(self):
        with tempfile.TemporaryDirectory() as directory:
            css = Path(directory, "theme.css")
            css.write_text('[data-tn-role="body"] { color: black; }', encoding="utf-8")
            config = _config(
                directory,
                output={
                    "bilingual": {"enabled": False},
                    "override_theme": {"styles": str(css)},
                },
            )
            source = _source(directory)
            app = Application(config, client=FakeClient(handler=routing_handler))
            translated = app.run_all(source, out_format="txt")
            first_path = Path(directory, "first.epub")
            app.assemble(translated["store"], source, out_path=str(first_path))
            first_bytes = first_path.read_bytes()
            targets = [
                segment.target for segment in translated["store"].load_chapter(0).text_segments
            ]

            copy_css = Path(directory, "copy.css")
            copy_css.write_bytes(css.read_bytes())
            first_bundle = resolve_theme(str(css), None)
            copied_bundle = resolve_theme(str(copy_css), None)
            digest_args = {
                "out_format": "epub",
                "mono": True,
                "bilingual": False,
                "bilingual_order": "target_first",
            }
            self.assertEqual(
                semantic_output_digest(first_bundle, **digest_args),
                semantic_output_digest(copied_bundle, **digest_args),
            )

            css.write_text('[data-tn-role="body"] { color: navy; }', encoding="utf-8")
            offline = FakeClient(handler=lambda *_args: self.fail("仅主题变化不得调用模型"))
            second_path = Path(directory, "second.epub")
            Application(config, client=offline).assemble(
                translated["store"], source, out_path=str(second_path)
            )
            self.assertNotEqual(second_path.read_bytes(), first_bytes)
            self.assertEqual(offline.calls, [])
            self.assertEqual(
                [segment.target for segment in translated["store"].load_chapter(0).text_segments],
                targets,
            )

    def test_v1_rules_snapshot_is_discarded_and_current_css_analyzes_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            source = _source(directory)
            translated = Application(
                _config(directory),
                client=FakeClient(handler=routing_handler),
            ).run_all(source, out_format="txt")
            store = translated["store"]
            _write_v1_selection(store)
            current = _config(
                directory,
                output={
                    "bilingual": {"enabled": False},
                    "override_theme": {"styles": "builtin:chinese-reading"},
                },
            )
            client = FakeClient(handler=routing_handler)

            outputs = Application(current, client=client).assemble(
                store,
                source,
                out_path=str(Path(directory, "current.epub")),
            )

            self.assertEqual(len(outputs), 1)
            self.assertIn("layout.classify", {call["operation"] for call in client.calls})
            saved = load_output_selection(
                store,
                source_bytes_sha256=store.load_state().identity.source_bytes_sha256,
            )
            self.assertEqual(saved.schema_version, 2)
            self.assertNotIn("rules", saved.selection["override_theme"])

    def test_v1_rules_snapshot_with_explicit_null_never_calls_layout_model(self):
        with tempfile.TemporaryDirectory() as directory:
            source = _source(directory)
            translated = Application(
                _config(directory),
                client=FakeClient(handler=routing_handler),
            ).run_all(source, out_format="txt")
            store = translated["store"]
            _write_v1_selection(store)
            current = _config(
                directory,
                output={
                    "bilingual": {"enabled": False},
                    "override_theme": None,
                },
            )
            offline = FakeClient(handler=lambda *_args: self.fail("显式 null 不得调用布局模型"))

            outputs = Application(current, client=offline).assemble(
                store,
                source,
                out_path=str(Path(directory, "plain.epub")),
            )

            self.assertEqual(len(outputs), 1)
            self.assertEqual(offline.calls, [])
            saved = load_output_selection(
                store,
                source_bytes_sha256=store.load_state().identity.source_bytes_sha256,
            )
            self.assertEqual(saved.schema_version, 2)
            self.assertIsNone(saved.selection["override_theme"])

    def test_identity_mismatch_and_failed_assembly_preserve_saved_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(directory, output={"bilingual": {"enabled": False}})
            source = _source(directory)
            result = Application(config, client=FakeClient(handler=routing_handler)).run_all(
                source, out_format="txt"
            )
            store = result["store"]
            selection_path = Path(store.run_dir, "output_selection.json")
            selection_before = selection_path.read_bytes()
            node_output_before = dict(store.load_state().nodes["assemble"].output)

            with (
                patch(
                    "trans_novel.pipeline.nodes.finish.assemble_outputs",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(RequiredNodeFailed) as raised,
            ):
                Application(config, client=FakeClient()).assemble(
                    store, source, out_format="txt", out_path=str(Path(directory, "failed.txt"))
                )
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(store.load_state().nodes["assemble"].output, node_output_before)

            Path(source).write_text("Changed source bytes.", encoding="utf-8")
            with self.assertRaises(IdentityMismatchError):
                Application(config, client=FakeClient()).assemble(store, source, out_format="txt")
            self.assertEqual(selection_path.read_bytes(), selection_before)


if __name__ == "__main__":
    unittest.main()
