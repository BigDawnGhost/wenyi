"""CLI 配置覆盖行为测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import typer
from rich.progress import Progress
from typer.testing import CliRunner

from trans_novel.cli import (
    _apply_store_languages,
    _configure_windows_console,
    _EstimatedRemainingColumn,
    _llm_stage_label,
    _RichProgressBridge,
    _validate_pdf_engine,
    app,
)
from trans_novel.config import Config
from trans_novel.ingest.errors import MinerUError


class FakeStore:
    run_dir = "state/book"

    def load_usage(self):
        return None


class TestCliConfig(unittest.TestCase):
    @staticmethod
    def _stage_task(progress, bridge):
        return next(task for task in progress.tasks if task.id == bridge.task)

    @staticmethod
    def _overall_task(progress, bridge):
        return next(task for task in progress.tasks if task.id == bridge.overall_task)

    def test_progress_bridge_reuses_stage_task_across_review_stages(self):
        progress = Progress(disable=True)
        bridge = _RichProgressBridge(progress, "准备全书审校…")

        bridge(0, 6386, "全书审校 R1")
        bridge(6386, 6386, "全书审校 R1")
        bridge(0, 58, "影子修订 R1")
        bridge(58, 58, "影子修订 R1")
        bridge(0, 6386, "全书盲审 R2")

        self.assertEqual(len(progress.tasks), 2)
        task = self._stage_task(progress, bridge)
        self.assertEqual(task.description, "全书盲审 R2 · LLM: idle")
        self.assertEqual(task.completed, 0)
        self.assertEqual(task.total, 6386)
        self.assertIsNone(self._overall_task(progress, bridge).total)

    def test_llm_stage_labels_include_known_and_readable_fallbacks(self):
        self.assertEqual(_llm_stage_label("Translator"), "Translating text")
        self.assertEqual(_llm_stage_label("custom_stage"), "Custom stage")
        self.assertEqual(_llm_stage_label("NewReviewAgent"), "New Review Agent")
        self.assertEqual(_llm_stage_label(None), "Model request")

    def test_progress_bridge_aggregates_concurrent_activity_and_returns_idle(self):
        progress = Progress(disable=True)
        bridge = _RichProgressBridge(progress, "预扫章节梗概")

        bridge.on_llm_activity(
            "request_started", request_id="r1", stage="Synopsizer", tier="fast"
        )
        bridge.on_llm_activity(
            "request_started", request_id="r2", stage="Synopsizer", tier="fast"
        )
        bridge.on_llm_activity(
            "request_started", request_id="r3", stage="Translator", tier="strong"
        )
        description = self._stage_task(progress, bridge).description
        self.assertIn("LLM: Translating text [strong]", description)
        self.assertIn("+2 other requests", description)

        bridge.on_llm_activity(
            "request_retry_wait",
            request_id="r1",
            stage="Synopsizer",
            tier="fast",
            attempt=2,
            max_attempts=5,
            wait_seconds=3.2,
            reason="http_502",
        )
        description = self._stage_task(progress, bridge).description
        self.assertIn(
            "LLM: Retrying Building book understanding [fast] 2/5 in 3.2s ×2",
            description,
        )
        self.assertIn("+1 other request", description)

        for request_id in ("r2", "r1", "r3"):
            bridge.on_llm_activity("request_finished", request_id=request_id)
        self.assertTrue(
            self._stage_task(progress, bridge).description.endswith("LLM: idle")
        )

    def test_activity_changes_do_not_change_counts_or_eta_samples(self):
        clock = [0.0]
        progress = Progress(disable=True, get_time=lambda: clock[0])
        bridge = _RichProgressBridge(progress, "第一章")
        bridge(0, 10, "第一章")
        clock[0] = 5.0
        bridge(2, 10, "第一章")
        task = self._stage_task(progress, bridge)
        completed = task.completed
        samples = list(task._progress)

        bridge.on_llm_activity(
            "request_started", request_id="r1", stage="Translator", tier="strong"
        )
        bridge.on_llm_activity("request_finished", request_id="r1")

        task = self._stage_task(progress, bridge)
        self.assertEqual(task.completed, completed)
        self.assertEqual(list(task._progress), samples)

    def test_eta_is_calculated_and_stage_rollback_clears_speed(self):
        clock = [0.0]
        progress = Progress(disable=True, get_time=lambda: clock[0])
        bridge = _RichProgressBridge(progress, "全书审校 R1")
        eta = _EstimatedRemainingColumn()

        bridge(0, 10, "全书审校 R1")
        self.assertIn("计算中", eta.render(self._stage_task(progress, bridge)).plain)
        clock[0] = 5.0
        bridge(2, 10, "全书审校 R1")
        clock[0] = 10.0
        bridge(4, 10, "全书审校 R1")
        self.assertEqual(
            eta.render(self._stage_task(progress, bridge)).plain,
            "阶段剩余 00:15",
        )

        bridge(0, 10, "全书盲审 R2")
        self.assertIn("计算中", eta.render(self._stage_task(progress, bridge)).plain)

    def test_translation_uses_local_stage_and_preserves_overall_progress(self):
        progress = Progress(disable=True)
        bridge = _RichProgressBridge(
            progress,
            "准备中…",
            overall_predictable=True,
        )

        bridge.update_translation(2, 10, 12, 100, "第一章")
        stage = self._stage_task(progress, bridge)
        overall = self._overall_task(progress, bridge)
        self.assertEqual((stage.completed, stage.total), (2, 10))
        self.assertEqual((overall.completed, overall.total), (12, 100))
        overall_task_id = bridge.overall_task

        bridge.update_translation(0, 5, 20, 100, "第二章")
        stage = self._stage_task(progress, bridge)
        overall = self._overall_task(progress, bridge)
        self.assertEqual((stage.completed, stage.total), (0, 5))
        self.assertEqual(bridge.overall_task, overall_task_id)
        self.assertEqual((overall.completed, overall.total), (20, 100))

        bridge(0, 0, "翻译章节标题…")
        overall = self._overall_task(progress, bridge)
        self.assertEqual(bridge.overall_task, overall_task_id)
        self.assertEqual((overall.completed, overall.total), (20, 100))

    def test_pdf_engine_validation_accepts_both_backends(self):
        self.assertEqual(_validate_pdf_engine("WeasyPrint"), "weasyprint")
        self.assertEqual(_validate_pdf_engine(" fpdf2 "), "fpdf2")

    def test_pdf_engine_validation_rejects_unknown_backend(self):
        with self.assertRaises(typer.Exit) as raised:
            _validate_pdf_engine("unknown")

        self.assertEqual(raised.exception.exit_code, 2)

    def test_standalone_tools_restore_manifest_languages(self):
        cfg = Config.from_dict({"language": {"source": "auto", "target": "zh"}})

        class Store:
            @staticmethod
            def load_manifest():
                return {"source_lang": "ru", "target_lang": "en"}

        _apply_store_languages(cfg, Store())

        self.assertEqual(cfg.source_lang, "ru")
        self.assertEqual(cfg.target_lang, "en")

    def test_qa_binds_retry_events_to_book_log(self):
        """独立 QA 的 provider 重试事件也必须写入当前书籍日志。"""
        cfg = Config.from_dict({"llm": {"provider": "fake"}})
        recorded: list[tuple[str, dict[str, object]]] = []

        class Store:
            glossary_path = "glossary.db"

            @staticmethod
            def exists():
                return True

            @staticmethod
            def load_manifest():
                return {"source_lang": "en", "target_lang": "zh"}

            @staticmethod
            def log_event(event, **data):
                recorded.append((event, data))

        class Client:
            sink = None

            def set_event_sink(self, sink):
                self.sink = sink

        class Glossary:
            def __init__(self, path):
                self.path = path

            def close(self):
                pass

        client = Client()

        class Checker:
            def __init__(self, configured_client, config):
                del config
                self.client = configured_client

            def check(self, store, glossary):
                del store, glossary
                self.client.sink("llm_retry_wait", reason="http_502")
                return []

        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.cli._runstore_for", return_value=Store()),
            patch("trans_novel.llm.factory.build_client", return_value=client),
            patch("trans_novel.glossary.store.GlossaryStore", Glossary),
            patch("trans_novel.agents.consistency.ConsistencyChecker", Checker),
            patch("trans_novel.cli._validate_api_configuration"),
        ):
            result = CliRunner().invoke(app, ["qa", "input.epub"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(recorded, [("llm_retry_wait", {"reason": "http_502"})])

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

    def test_api_preflight_covers_model_commands(self):
        for command in (
            "translate",
            "prepare",
            "review",
            "qa",
        ):
            with self.subTest(command=command):
                with patch(
                    "trans_novel.cli._validate_api_configuration",
                    side_effect=RuntimeError("missing key"),
                ) as validate:
                    result = CliRunner().invoke(app, [command])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIn("missing key", result.output)
                validate.assert_called_once_with()

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
                self.assertIn("输入文件不存在", result.output)
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
                "llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}},
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                captured["config"] = config

            def run_review(self, input_path, **kwargs):
                captured["input_path"] = input_path
                captured["kwargs"] = kwargs
                progress = kwargs["progress"]
                progress(0, 4, "全书审校 R1")
                progress(2, 4, "全书审校 R1")
                progress(4, 4, "全书审校 R1")
                progress(0, 1, "影子修订 R1")
                progress(1, 1, "影子修订 R1")
                progress(0, 4, "全书盲审 R2")
                progress(4, 4, "全书盲审 R2")
                progress(1, 2, "干净确认")
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
        self.assertIn("仍有 1 项问题", result.output)
        self.assertIn("生成 1 项修改建议", result.output)
        self.assertIn("/tmp/reviews/review-20260801-120000", result.output)
        self.assertIn("干净确认", result.output)

    def test_translate_reports_missing_api_key_before_inspecting_input(self):
        missing = os.path.join(tempfile.gettempdir(), "trans-novel-missing.epub")
        cfg = Config.from_dict({"llm": {"provider": "deepseek"}})
        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.cli.os.path.isfile") as isfile,
            patch.dict(os.environ, {}, clear=True),
        ):
            result = CliRunner().invoke(app, ["translate", missing])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("DEEPSEEK_API_KEY", result.output)
        self.assertNotIn("输入文件不存在", result.output)
        self.assertNotIn("Traceback", result.output)
        isfile.assert_not_called()

    def test_assemble_skips_api_preflight(self):
        cfg = Config.from_dict({"llm": {"provider": "deepseek"}})
        with (
            patch("trans_novel.cli._load_config", return_value=cfg),
            patch("trans_novel.cli.os.path.isfile", return_value=False),
            patch.dict(os.environ, {}, clear=True),
        ):
            result = CliRunner().invoke(app, ["assemble", "missing.epub"])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("输入文件不存在", result.output)
        self.assertNotIn("DEEPSEEK_API_KEY", result.output)

    def test_translate_expected_errors_are_printed_without_traceback(self):
        cfg = Config.from_dict({"llm": {"provider": "fake", "tiers": {"strong": {"model": "p"}}}})

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

    def test_translate_rejects_unknown_output_format_after_api_preflight(self):
        cfg = Config.from_dict({"llm": {"provider": "fake"}})
        with (
            patch("trans_novel.cli.os.path.isfile", return_value=True),
            patch("trans_novel.cli._load_config", return_value=cfg),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--format", "docx"])

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
                    "llm": {"provider": "fake"},
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
