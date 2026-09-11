"""CLI configuration override tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import typer
from rich.cells import cell_len
from rich.progress import Progress
from typer.testing import CliRunner

from trans_novel.cli import (
    _configure_windows_console,
    _progress_columns,
    _RichProgressBridge,
    _validate_pdf_engine,
    _WorkflowElapsedColumn,
    app,
)
from trans_novel.config import Config
from trans_novel.ingest.errors import MinerUError
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pdf_bridge import BabeldocBridgeError


class FakeStore:
    run_dir = "state/book"

    def load_usage(self):
        return None


class TestCliConfig(unittest.TestCase):
    def test_progress_clock_advances_after_completed_stage(self):
        now = 10.0
        progress = Progress(disable=True, get_time=lambda: now)
        bridge = _RichProgressBridge(progress, "Preparing translation…")
        bridge(1674, 1674, "Translation complete")
        self.assertTrue(progress.tasks[0].finished)

        now = 20.0
        bridge(0, 1674, "Whole-book review R1")
        bridge(777, 1674, "Whole-book review R1")
        task = progress.tasks[0]
        self.assertFalse(task.finished)
        now = 25.0
        self.assertEqual(_WorkflowElapsedColumn().render(task).plain, "0:00:15")
        now = 35.0
        self.assertEqual(_WorkflowElapsedColumn().render(task).plain, "0:00:25")
        bridge(778, 1674, "Whole-book review R1")
        self.assertEqual(_WorkflowElapsedColumn().render(task).plain, "0:00:25")

    def test_indeterminate_progress_updates_preserve_elapsed_time(self):
        now = 10.0
        progress = Progress(disable=True, get_time=lambda: now)
        bridge = _RichProgressBridge(progress, "Preparing review…")
        bridge(1, 1, "Loading review chapters")
        now = 12.0
        bridge(0, 0, "Restoring review checkpoint…")
        now = 15.0
        bridge(0, 0, "Restoring review checkpoint…")
        task = progress.tasks[0]
        self.assertIsNone(task.total)
        self.assertFalse(task.finished)
        self.assertEqual(_WorkflowElapsedColumn().render(task).plain, "0:00:05")

    def test_progress_clock_keeps_running_while_completed_stage_waits(self):
        now = 10.0
        progress = Progress(disable=True, get_time=lambda: now)
        bridge = _RichProgressBridge(progress, "Preparing…")
        now = 15.0
        bridge(2, 2, "Translating chapter 1")
        now = 25.0
        self.assertEqual(_WorkflowElapsedColumn().render(progress.tasks[0]).plain, "0:00:15")
        bridge(1, 3, "Translating chapter 2")
        now = 30.0
        self.assertEqual(_WorkflowElapsedColumn().render(progress.tasks[0]).plain, "0:00:20")

    def test_long_progress_description_is_ellipsized_without_hiding_bar(self):
        progress = Progress(*_progress_columns(), disable=True)
        bridge = _RichProgressBridge(progress, "Preparing…")

        bridge(1, 2, "这是一个特别特别长而且不应该挤掉右侧进度条的章节标题")

        description = progress.tasks[0].description
        self.assertTrue(description.endswith("…"))
        self.assertLessEqual(cell_len(description), 28)

    def test_progress_bridge_reuses_one_task_across_review_stages(self):
        progress = Progress(disable=True)
        bridge = _RichProgressBridge(progress, "Preparing whole-book review…")

        bridge(0, 6386, "Whole-book review R1")
        bridge(6386, 6386, "Whole-book review R1")
        bridge(0, 58, "Shadow revision R1")
        bridge(58, 58, "Shadow revision R1")
        bridge(0, 6386, "Blind whole-book review R2")

        self.assertEqual(len(progress.tasks), 1)
        task = progress.tasks[0]
        self.assertEqual(task.description, "Blind whole-book review R2")
        self.assertEqual(task.completed, 0)
        self.assertEqual(task.total, 6386)
        self.assertFalse(task.finished)

    def test_pdf_engine_validation_accepts_both_backends(self):
        self.assertEqual(_validate_pdf_engine("WeasyPrint"), "weasyprint")
        self.assertEqual(_validate_pdf_engine(" fpdf2 "), "fpdf2")

    def test_pdf_engine_validation_rejects_unknown_backend(self):
        with self.assertRaises(typer.Exit) as raised:
            _validate_pdf_engine("unknown")

        self.assertEqual(raised.exception.exit_code, 2)

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

    def test_version_reads_installed_package_metadata(self):
        with patch("trans_novel.cli.package_version", return_value="0.3.5"):
            result = CliRunner().invoke(app, ["--version"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.strip(), "0.3.5")

    def test_translate_defaults_keep_config_switches(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "p"}},
                },
                "pipeline": {"polish": True},
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                self.client = FakeClient()
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
        self.assertTrue(captured["review"])

    def test_translate_flags_override_config_switches(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "p"}},
                },
                "pipeline": {"polish": True},
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                self.client = FakeClient()
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
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(captured["polish"])
        self.assertTrue(captured["review"])

    def test_prepare_stops_before_translation(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "p"}},
                },
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
                self.client = FakeClient()
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
        self.assertIn("Preparation complete", result.output)
        self.assertIn("prescanned 2/2 chapters", result.output)

    def test_translate_chapter_rejects_finish_options(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "p"}},
                },
            }
        )
        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                ["translate", "input.txt", "--chapter", "0", "--review"],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        # CliRunner may wrap the message on Windows; compare ignoring whitespace.
        compact = "".join(result.output.split())
        self.assertIn("--chapteronlytranslatesandsavestheselectedchapter", compact)
        self.assertIn("--review/--no-review", compact)

    def test_top_level_help_exposes_workflow_without_duplicate_aliases(self):
        result = CliRunner().invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        for command in (
            "translate",
            "prepare",
            "review",
            "report",
            "assemble",
            "status",
            "glossary",
        ):
            self.assertIn(command, result.output)
        self.assertNotRegex(result.output, r"(?m)^│\s*resume\s{2,}")
        self.assertNotRegex(result.output, r"(?m)^│\s*tools\s{2,}")

    def test_glossary_help_exposes_action_subcommands(self):
        result = CliRunner().invoke(app, ["glossary", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("list", result.output)
        self.assertIn("conflicts", result.output)
        self.assertIn("resolve", result.output)

    def test_api_preflight_covers_model_commands(self):
        for command in (
            "translate",
            "prepare",
            "review",
        ):
            with self.subTest(command=command):
                with patch(
                    "trans_novel.cli._validate_api_configuration",
                    side_effect=RuntimeError("missing key"),
                ) as validate:
                    result = CliRunner().invoke(app, [command, "input.txt"])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("missing key", result.output)
                self.assertEqual(validate.call_count, 1)

    def test_api_preflight_skips_local_commands(self):
        for args in (
            ["status", "missing.txt"],
            ["report", "missing.txt"],
            ["glossary", "list", "missing.txt"],
            ["glossary", "conflicts", "missing.txt"],
            [
                "glossary",
                "resolve",
                "missing.txt",
                "source",
                "target",
            ],
            ["assemble", "missing.txt"],
        ):
            with self.subTest(args=args):
                with patch(
                    "trans_novel.cli._validate_api_configuration",
                    side_effect=AssertionError(f"{args} must not validate credentials"),
                ) as validate:
                    result = CliRunner().invoke(app, args)
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Input file does not exist", result.output)
                validate.assert_not_called()

    def test_api_preflight_skips_help_at_every_level(self):
        for args in (["--help"], ["translate", "--help"], ["glossary", "--help"]):
            with self.subTest(args=args):
                with patch(
                    "trans_novel.cli._validate_api_configuration",
                    side_effect=AssertionError("help must not validate credentials"),
                ) as validate:
                    result = CliRunner().invoke(app, args)
                self.assertEqual(result.exit_code, 0, result.output)
                validate.assert_not_called()

    def test_review_command_runs_full_read_only_review(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "p"}},
                },
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                self.client = FakeClient()
                captured["config"] = config

            def run_review(self, input_path, **kwargs):
                captured["input_path"] = input_path
                captured["kwargs"] = kwargs
                progress = kwargs["progress"]
                progress(0, 4, "Whole-book review R1")
                progress(2, 4, "Whole-book review R1")
                progress(4, 4, "Whole-book review R1")
                progress(0, 1, "Shadow revision R1")
                progress(1, 1, "Shadow revision R1")
                progress(0, 4, "Blind whole-book review R2")
                progress(4, 4, "Blind whole-book review R2")
                progress(1, 2, "Clean confirmation")
                return {
                    "store": FakeStore(),
                    "review_issues": [{"index": 0, "type": "missing"}],
                    "review_changes": [{"chapter": 0, "index": 0}],
                    "review_result": {
                        "termination": "max_rounds",
                        "summary": {"issue_count": 1, "change_count": 1},
                    },
                    "review_dir": "/tmp/reviews/review-20260801-120000",
                }

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["review", "input.txt"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["input_path"], "input.txt")
        self.assertIn("progress", captured["kwargs"])
        self.assertIn("max_rounds", result.output)
        self.assertIn("Remaining issues: 1", result.output)
        self.assertIn("suggested changes: 1", " ".join(result.output.split()))
        self.assertIn("/tmp/reviews/review-20260801-120000", result.output)
        self.assertIn("Clean confirmation", result.output)

    def test_review_autofix_option_overrides_config_and_reports_writeback(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "p"}},
                },
                "pipeline": {"review_autofix": False},
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                self.client = FakeClient()
                captured["autofix"] = config.pipeline.review_autofix

            def run_review(self, input_path, **kwargs):
                return {
                    "store": FakeStore(),
                    "review_result": {
                        "termination": "max_rounds",
                        "summary": {"issue_count": 1, "change_count": 1},
                        "autofix": {
                            "enabled": True,
                            "status": "partial",
                            "applied_segment_count": 1,
                            "failed_issue_count": 1,
                        },
                    },
                    "review_dir": "/tmp/reviews/review-autofix",
                }

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["review", "input.txt", "--autofix"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(captured["autofix"])
        self.assertIn("Autofix: published 1 paragraphs, failed issues: 1", result.output)

    def test_translate_reports_missing_api_key_before_inspecting_input(self):
        missing = os.path.join(tempfile.gettempdir(), "trans-novel-missing.epub")
        cfg = Config.from_dict({"llm": {"preset": "deepseek"}})
        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.cli._require_input_file") as require_input,
            patch.dict(os.environ, {}, clear=True),
        ):
            result = CliRunner().invoke(app, ["translate", missing])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("DEEPSEEK_API_KEY", result.output)
        self.assertNotIn("Input file does not exist", result.output)
        self.assertNotIn("Traceback", result.output)
        require_input.assert_not_called()

    def test_assemble_skips_api_preflight(self):
        cfg = Config.from_dict({"llm": {"preset": "deepseek"}})
        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.cli.os.path.isfile", return_value=False),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = CliRunner().invoke(app, ["assemble", "missing.epub"])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Input file does not exist", result.output)
        self.assertNotIn("DEEPSEEK_API_KEY", result.output)

    def test_assemble_uses_local_orchestrator_entry(self):
        cfg = Config.from_dict({"llm": {"preset": "fake"}})
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config, client=None):
                self.client = FakeClient()
                del client
                captured["mono"] = config.output.mono
                captured["bilingual"] = config.output.bilingual

            def run_assemble(self, input_path, **kwargs):
                captured["input"] = input_path
                captured["kwargs"] = kwargs
                return {"store": FakeStore(), "outputs": ["out.pdf"]}

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                [
                    "assemble",
                    "input.epub",
                    "--format",
                    "pdf",
                    "--pdf-engine",
                    "fpdf2",
                    "--no-mono",
                    "--bilingual",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertFalse(captured["mono"])
        self.assertTrue(captured["bilingual"])
        self.assertEqual(captured["input"], "input.epub")
        self.assertEqual(captured["kwargs"]["out_format"], "pdf")
        self.assertEqual(captured["kwargs"]["pdf_engine"], "fpdf2")
        self.assertIn("out.pdf", result.output)

    def test_report_uses_local_orchestrator_entry(self):
        cfg = Config.from_dict({"llm": {"preset": "fake"}})
        captured = {}

        class ReportStore:
            report_path = "state/book/report.json"

        class FakeOrchestrator:
            def __init__(self, config, client=None):
                self.client = FakeClient()
                del client
                captured["config"] = config

            def run_report(self, input_path):
                captured["input"] = input_path
                return {
                    "store": ReportStore(),
                    "report": {
                        "summary": {
                            "chapters_done": 2,
                            "chapters_total": 2,
                            "terms": 3,
                            "open_conflicts": 0,
                            "empty_targets": 0,
                        }
                    },
                }

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["report", "input.epub"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["input"], "input.epub")
        self.assertIn("state/book/report.json", result.output)

    def test_translate_expected_errors_are_printed_without_traceback(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "preset": "fake",
                    "models": {"default_strong": {"provider": "default", "model": "p"}},
                }
            }
        )

        for error in (
            MinerUError("未设置 MINERU_API_KEY"),
            BabeldocBridgeError("BabelDOC 检测到纯图片 PDF，请改用 MinerU"),
            ValueError("Unsupported output format：xml"),
        ):
            with self.subTest(error=type(error).__name__):

                class FakeOrchestrator:
                    def __init__(self, config):
                        self.client = FakeClient()
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

    def test_translate_rejects_unknown_output_format_after_api_preflight(self):
        cfg = Config.from_dict({"llm": {"preset": "fake"}})
        with (
            patch("trans_novel.cli.os.path.isfile", return_value=True),
            patch("trans_novel.cli._load_config", return_value=cfg),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--format", "xml"])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("Unsupported output format", result.output)

    def test_translate_reports_out_of_range_chapter_without_traceback(self):
        cfg = Config.from_dict({"llm": {"preset": "fake"}})

        class FakeOrchestrator:
            def __init__(self, config):
                self.client = FakeClient()
                pass

            def run(self, input_path, **kwargs):
                raise ValueError("章节编号 9 不存在；可用范围：0–1")

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.pipeline.orchestrator.Orchestrator", FakeOrchestrator),
            patch("trans_novel.cli.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--chapter", "9"])

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
                    "llm": {"preset": "fake"},
                    "paths": {"state_dir": state_dir},
                }
            )

            with patch("trans_novel.cli._load_config", return_value=cfg):
                result = CliRunner().invoke(app, ["status", src])

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("No progress found", result.output)
            self.assertFalse(os.path.exists(state_dir))

    def test_state_commands_print_source_identity_errors(self):
        commands = (
            ["status", "book.epub"],
            ["glossary", "list", "book.epub"],
            ["glossary", "conflicts", "book.epub"],
            ["glossary", "resolve", "book.epub", "source", "target"],
        )
        for args in commands:
            with self.subTest(args=args):
                with (
                    patch("trans_novel.cli.os.path.isfile", return_value=True),
                    patch(
                        "trans_novel.cli._runstore_for",
                        side_effect=ValueError("Input content does not match existing state"),
                    ),
                ):
                    result = CliRunner().invoke(app, args)

                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("Error: Input content does not match existing state", result.output)
                self.assertNotIn("Traceback", result.output)


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
