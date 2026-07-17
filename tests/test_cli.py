"""CLI 配置覆盖行为测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from trans_novel.cli import _apply_store_languages, _configure_windows_console, app
from trans_novel.config import Config
from trans_novel.ingest.errors import MinerUError


class FakeStore:
    run_dir = "state/book"

    def load_usage(self):
        return None


class TestCliConfig(unittest.TestCase):
    def test_standalone_tools_restore_manifest_languages(self):
        cfg = Config.from_dict(
            {"language": {"source": "auto", "target": "zh"}}
        )

        class Store:
            @staticmethod
            def load_manifest():
                return {"source_lang": "ru", "target_lang": "en"}

        _apply_store_languages(cfg, Store())

        self.assertEqual(cfg.source_lang, "ru")
        self.assertEqual(cfg.target_lang, "en")

    def test_every_cli_start_checks_default_config(self):
        runner = CliRunner()
        with patch.object(Config, "create_default_file", return_value=True) as create:
            result = runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        create.assert_called_once_with("config.yaml")

    def test_cli_start_respects_custom_config_path(self):
        runner = CliRunner()
        with patch.object(Config, "create_default_file", return_value=True) as create:
            result = runner.invoke(
                app,
                ["--config", "settings/config.yaml", "--help"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        create.assert_called_once_with("settings/config.yaml")

    def test_translate_defaults_keep_config_switches(self):
        cfg = Config.from_dict(
            {
                "llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}},
                "pipeline": {"polish": True, "consistency_qa": False},
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                captured["polish"] = config.pipeline.polish
                captured["review"] = config.pipeline.review

            def run_all(self, input_path, **kwargs):
                captured["run_all"] = kwargs
                return {
                    "report": {
                        "summary": {
                            "chapters_done": 1,
                            "chapters_total": 1,
                            "terms": 0,
                        }
                    },
                    "audit": [],
                    "qa_issues": [],
                    "output": "out.epub",
                    "store": FakeStore(),
                }

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(captured["polish"])
        self.assertFalse(captured["review"])
        self.assertIsNone(captured["run_all"]["do_qa"])

    def test_translate_flags_override_config_switches(self):
        cfg = Config.from_dict(
            {
                "llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}},
                "pipeline": {"polish": True, "consistency_qa": False},
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                captured["polish"] = config.pipeline.polish
                captured["review"] = config.pipeline.review

            def run_all(self, input_path, **kwargs):
                captured["run_all"] = kwargs
                return {
                    "report": {
                        "summary": {
                            "chapters_done": 1,
                            "chapters_total": 1,
                            "terms": 0,
                        }
                    },
                    "audit": [],
                    "qa_issues": [],
                    "output": "out.epub",
                    "store": FakeStore(),
                }

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                [
                    "translate",
                    "input.txt",
                    "--no-polish",
                    "--review",
                    "--qa",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(captured["polish"])
        self.assertTrue(captured["review"])
        self.assertTrue(captured["run_all"]["do_qa"])

    def test_prepare_stops_before_translation(self):
        cfg = Config.from_dict(
            {
                "llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}},
            }
        )
        captured = {}

        class PreparedStore(FakeStore):
            @staticmethod
            def load_manifest():
                return {"chapters": [{"index": 0}, {"index": 1}]}

            @staticmethod
            def load_analysis():
                return {"book_synopsis": "overview"}

            @staticmethod
            def load_chapter(index):
                class Chapter:
                    meta = {"source_digest": f"digest-{index}"}

                return Chapter()

        class FakeOrchestrator:
            def __init__(self, config):
                captured["config"] = config

            def prepare_for_translation(self, input_path, **kwargs):
                captured["input_path"] = input_path
                captured["prepare"] = kwargs
                return PreparedStore()

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                ["prepare", "input.txt"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["input_path"], "input.txt")
        self.assertIn("准备完成", result.output)
        self.assertIn("预扫 2/2 章", result.output)

    def test_translate_chapter_rejects_finish_options(self):
        cfg = Config.from_dict(
            {
                "llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}},
            }
        )
        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                ["translate", "input.txt", "--chapter", "0", "--qa"],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("--chapter 只翻译并保存指定章节", result.output)
        self.assertIn("--qa/--no-qa", result.output)

    def test_top_level_help_exposes_workflow_without_duplicate_aliases(self):
        result = CliRunner().invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        for command in (
            "translate",
            "prepare",
            "review",
            "qa",
            "report",
            "assemble",
            "status",
            "glossary",
        ):
            self.assertIn(command, result.output)
        self.assertNotIn("resume", result.output)
        self.assertNotIn("tools", result.output)

    def test_glossary_help_exposes_action_subcommands(self):
        result = CliRunner().invoke(app, ["glossary", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("list", result.output)
        self.assertIn("conflicts", result.output)
        self.assertIn("resolve", result.output)

    def test_review_command_runs_final_review_with_overrides(self):
        cfg = Config.from_dict(
            {
                "llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}},
                "pipeline": {"autofix_severe": False},
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                captured["config"] = config

            def run_review(self, input_path, **kwargs):
                captured["input_path"] = input_path
                captured["kwargs"] = kwargs
                return {
                    "store": FakeStore(),
                    "review_issues": [{"index": 0, "type": "missing"}],
                }

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                ["review", "input.txt", "--force", "--fix"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["input_path"], "input.txt")
        self.assertTrue(captured["kwargs"]["force"])
        self.assertTrue(captured["kwargs"]["autofix"])
        self.assertIn("发现 1 项问题", result.output)

    def test_translate_missing_input_exits_before_loading_config(self):
        missing = os.path.join(tempfile.gettempdir(), "trans-novel-missing.epub")
        with patch(
            "trans_novel.cli._load_config",
            side_effect=AssertionError("config should not load"),
        ):
            result = CliRunner().invoke(app, ["translate", missing])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("输入文件不存在", result.output)

    def test_translate_expected_errors_are_printed_without_traceback(self):
        cfg = Config.from_dict(
            {"llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}}}
        )

        for error in (
            MinerUError("未设置 MINERU_API_KEY"),
            ValueError("不支持的输出格式：xml"),
        ):
            with self.subTest(error=type(error).__name__):
                class FakeOrchestrator:
                    def __init__(self, config):
                        pass

                    def run_all(self, input_path, **kwargs):
                        raise error

                with (
                    patch("trans_novel.cli._load_config", return_value=cfg),
                    patch(
                        "trans_novel.pipeline.orchestrator.Orchestrator",
                        FakeOrchestrator,
                    ),
                    patch("trans_novel.cli.os.path.isfile", return_value=True),
                ):
                    result = CliRunner().invoke(app, ["translate", "input.pdf"])

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn(str(error), result.output)
                self.assertNotIn("Traceback", result.output)

    def test_translate_rejects_unknown_output_format_before_loading_config(self):
        with (
            patch("trans_novel.cli.os.path.isfile", return_value=True),
            patch(
                "trans_novel.cli._load_config",
                side_effect=AssertionError("config should not load"),
            ),
        ):
            result = CliRunner().invoke(
                app, ["translate", "input.txt", "--format", "pdf"]
            )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("不支持的输出格式", result.output)

    def test_translate_reports_out_of_range_chapter_without_traceback(self):
        cfg = Config.from_dict({"llm": {"provider": "fake"}})

        class FakeOrchestrator:
            def __init__(self, config):
                pass

            def run(self, input_path, **kwargs):
                raise ValueError("章节编号 9 不存在；可用范围：0–1")

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app, ["translate", "input.txt", "--chapter", "9"]
            )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("章节编号 9 不存在", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_status_does_not_create_state_directory(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "novel.txt")
            state_dir = os.path.join(d, "state")
            with open(src, "w", encoding="utf-8") as f:
                f.write("第一段。\n")
            cfg = Config.from_dict(
                {
                    "language": {"source": "ja", "target": "zh"},
                    "paths": {"state_dir": state_dir},
                }
            )

            with patch("trans_novel.cli._load_config", return_value=cfg):
                result = CliRunner().invoke(app, ["status", src])

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("尚无进度", result.output)
            self.assertFalse(os.path.exists(state_dir))


class TestWindowsConsoleEncoding(unittest.TestCase):
    class _Stream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    def test_configures_utf8_for_windows_streams(self):
        out = self._Stream()
        err = self._Stream()

        _configure_windows_console((out, err), is_windows=True)

        self.assertEqual(out.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(err.calls, [{"encoding": "utf-8", "errors": "replace"}])


if __name__ == "__main__":
    unittest.main()
