"""CLI 配置覆盖行为测试。"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.cli import app
from trans_novel.cli.common import configure_windows_console, print_chapter_processing
from trans_novel.config import Config
from trans_novel.pipeline.execution import RequiredNodeFailed

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    """去除 ANSI 转义序列，避免 CI 环境强制启用彩色输出时干扰字符串断言。"""

    return _ANSI_RE.sub("", output)


class FakeStore:
    def load_usage(self):
        return None

    def load_epub_verification(self):
        return None


class TestCliBootstrap(unittest.TestCase):
    def test_version_is_available_without_a_subcommand(self):
        result = CliRunner().invoke(app, ["--version"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertRegex(plain(result.output), r"^trans-novel \d+\.\d+\.\d+\s*$")

    def test_init_writes_a_loadable_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")

            result = CliRunner().invoke(app, ["--config", config_path, "init"])

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("已生成配置文件", plain(result.output))
            config = Config.load(config_path)
            self.assertEqual(
                config.llm.models.translator,
                ["openrouter/tencent/hy-mt2-30b-a3b:off"],
            )
            with open(config_path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), Config.default_config_text())

    def test_init_does_not_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as stream:
                stream.write("keep-me")

            result = CliRunner().invoke(app, ["--config", config_path, "init"])

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("配置文件已存在", plain(result.output))
            with open(config_path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "keep-me")

    def test_translate_uses_defaults_without_creating_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            result = CliRunner().invoke(
                app,
                ["--config", config_path, "translate", "missing.txt"],
            )

            output = plain(result.output)
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("输入文件不存在", output)
            self.assertFalse(os.path.exists(config_path))

    def test_invalid_config_has_concise_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "invalid.yaml")
            with open(config_path, "w", encoding="utf-8") as stream:
                stream.write("llm: [")
            with patch("trans_novel.cli.common.os.path.isfile", return_value=True):
                result = CliRunner().invoke(
                    app,
                    ["--config", config_path, "translate", "input.txt"],
                )

        output = plain(result.output)
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("配置文件无效", output)
        self.assertNotIn("Traceback", output)

    def test_unsupported_thinking_level_lists_supported_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as stream:
                stream.write(
                    "llm:\n"
                    "  models:\n"
                    "    translator:\n"
                    "      - opencode-go/deepseek-v4-flash:low\n"
                    "    analyst:\n"
                    "      - opencode-go/deepseek-v4-flash:off\n"
                    "    editor:\n"
                    "      - opencode-go/deepseek-v4-flash:off\n"
                    "    fast:\n"
                    "      - opencode-go/deepseek-v4-flash:off\n"
                )
            with patch("trans_novel.cli.common.os.path.isfile", return_value=True):
                result = CliRunner().invoke(
                    app,
                    ["--config", config_path, "translate", "input.txt"],
                )

        output = plain(result.output)
        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("deepseek-v4-flash 不支持", output)
        self.assertIn("'low'；支持：off, high, max", output)
        self.assertNotIn("Traceback", output)


class TestCliConfig(unittest.TestCase):
    def test_translate_defaults_keep_config_switches(self):
        cfg = Config.from_dict(
            {
                "llm": fake_llm_dict(),
                "quality": "quality",
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                captured["polish"] = config.pipeline.polish

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
            patch("trans_novel.cli.common.load_config", return_value=cfg),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(captured["polish"])

    def test_indeterminate_phase_does_not_reuse_translation_total(self):
        config = Config.from_dict({"llm": fake_llm_dict()})

        class FakeOrchestrator:
            def __init__(self, _config):
                pass

            def run_all(self, _input_path, **kwargs):
                progress = kwargs["progress"]
                progress(2, 2, "翻译完成")
                progress(0, 0, "生成译文文件…")
                return {
                    "report": {
                        "summary": {"chapters_done": 1, "chapters_total": 1, "terms": 0},
                        "repair": {},
                    },
                    "audit": [],
                    "qa_issues": [],
                    "output": "out.epub",
                    "store": FakeStore(),
                }

        with (
            patch("trans_novel.cli.common.load_config", return_value=config),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("生成译文文件", plain(result.output))
        self.assertNotIn("0/2", plain(result.output))
        self.assertNotIn("0/?", plain(result.output))

    def test_translate_flags_override_config_switches(self):
        cfg = Config.from_dict(
            {
                "llm": fake_llm_dict(),
                "quality": "quality",
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                captured["polish"] = config.pipeline.polish
                captured["quality"] = config.quality
                captured["source_language"] = config.source_lang
                captured["honorifics"] = config.honorific_strategy

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
            patch("trans_novel.cli.common.load_config", return_value=cfg),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                [
                    "translate",
                    "input.txt",
                    "--quality",
                    "economy",
                    "--polish",
                    "--source-language",
                    "en",
                    "--honorifics",
                    "drop",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(captured["polish"])
        self.assertEqual(captured["quality"], "economy")
        self.assertEqual(captured["source_language"], "en")
        self.assertEqual(captured["honorifics"], "drop")

    def test_removed_back_matter_flag_is_unknown(self):
        result = CliRunner().invoke(app, ["translate", "input.txt", "--back-matter", "full"])
        self.assertEqual(result.exit_code, 2)

    def test_translate_help_omits_back_matter(self):
        result = CliRunner().invoke(app, ["translate", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("back-matter", plain(result.output))

    def test_external_back_matter_config_is_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_dict({"back_matter": "full"})

    def test_translate_prepare_stops_before_translation(self):
        config = Config.from_dict(
            {
                "llm": fake_llm_dict(),
            }
        )
        captured = {}

        class PreparedStore(FakeStore):
            run_dir = "state/novel"

            @staticmethod
            def load_manifest():
                return {"chapters": [{"index": 0}, {"index": 1}]}

        class FakeOrchestrator:
            def __init__(self, loaded_config):
                captured["config"] = loaded_config

            def prepare_for_translation(self, input_path, **kwargs):
                captured["input_path"] = input_path
                captured["prepare"] = kwargs
                return PreparedStore()

            def run_all(self, input_path, **kwargs):
                raise AssertionError("--prepare must not start translation")

        with (
            patch("trans_novel.cli.common.load_config", return_value=config),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--prepare"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["input_path"], "input.txt")
        self.assertIn("准备完成", result.output)
        self.assertIn("解析 2 章", result.output)
        self.assertNotIn("预扫 2/2 章", result.output)

    def test_translate_required_node_failures_are_actionable(self):
        config = Config.from_dict({"llm": fake_llm_dict()})
        message = "required node failed: translate"
        resume = "运行状态已保存；再次运行相同命令即可从失败位置继续。"

        class FakeOrchestrator:
            def __init__(self, loaded_config):
                pass

            @staticmethod
            def _raise(*args, **kwargs):
                raise RequiredNodeFailed(message)

            prepare_for_translation = _raise
            run = _raise
            run_all = _raise

        cases = (
            ["translate", "input.txt", "--prepare"],
            ["translate", "input.txt", "--chapter", "0"],
            ["translate", "input.txt"],
        )
        with (
            patch("trans_novel.cli.common.load_config", return_value=config),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            for args in cases:
                with self.subTest(args=args):
                    result = CliRunner().invoke(app, args)
                    output = plain(result.output)
                    self.assertEqual(result.exit_code, 2, result.output)
                    self.assertIn(message, output)
                    self.assertIn(resume, output)
                    self.assertNotIn("Traceback", output)

    def test_translate_prepare_rejects_chapter(self):
        config = Config.from_dict(
            {
                "llm": fake_llm_dict(),
            }
        )
        with (
            patch("trans_novel.cli.common.load_config", return_value=config),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                ["translate", "input.txt", "--prepare", "--chapter", "0"],
            )

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("--prepare 不能与 --chapter", plain(result.output))

    def test_resume_delegates_to_translate_without_audit_argument(self):
        cfg = Config.from_dict(
            {
                "llm": fake_llm_dict(),
                "quality": "quality",
            }
        )
        captured = {}

        class FakeOrchestrator:
            def __init__(self, config):
                captured["polish"] = config.pipeline.polish

            def run_all(self, input_path, **kwargs):
                captured["input_path"] = input_path
                captured["run_all"] = kwargs
                return {
                    "report": {
                        "summary": {
                            "chapters_done": 1,
                            "chapters_total": 1,
                            "terms": 0,
                        }
                    },
                    "qa_issues": [],
                    "output": "out.txt",
                    "store": FakeStore(),
                }

        with (
            patch("trans_novel.cli.common.load_config", return_value=cfg),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(
                app,
                ["resume", "input.txt", "--format", "txt"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured["input_path"], "input.txt")
        self.assertEqual(captured["run_all"]["out_format"], "txt")
        self.assertIsNone(captured["run_all"]["out_path"])
        self.assertTrue(captured["polish"])

    def test_translate_missing_input_exits_before_loading_config(self):
        missing = os.path.join(tempfile.gettempdir(), "trans-novel-missing.epub")
        with patch(
            "trans_novel.cli.common.load_config",
            side_effect=AssertionError("config should not load"),
        ):
            result = CliRunner().invoke(app, ["translate", missing])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("输入文件不存在", result.output)

    def test_translate_rejects_unknown_output_format_before_loading_config(self):
        with (
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
            patch(
                "trans_novel.cli.common.load_config",
                side_effect=AssertionError("config should not load"),
            ),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--format", "pdf"])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("不支持的输出格式", result.output)

    def test_translate_reports_out_of_range_chapter_without_traceback(self):
        cfg = Config.from_dict({"llm": fake_llm_dict()})

        class FakeOrchestrator:
            def __init__(self, config):
                pass

            def run(self, input_path, **kwargs):
                raise ValueError("章节编号 9 不存在；可用范围：0–1")

        with (
            patch("trans_novel.cli.common.load_config", return_value=cfg),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
            patch("trans_novel.cli.common.os.path.isfile", return_value=True),
        ):
            result = CliRunner().invoke(app, ["translate", "input.txt", "--chapter", "9"])

        self.assertEqual(result.exit_code, 2, result.output)
        self.assertIn("章节编号 9 不存在", result.output)
        self.assertNotIn("Traceback", result.output)

    def test_tools_assemble_forwards_layout_refresh(self):
        config = Config.from_dict({"llm": fake_llm_dict()})
        captured = {}

        class Store(FakeStore):
            @staticmethod
            def exists():
                return True

        class FakeOrchestrator:
            def __init__(self, _config):
                pass

            def assemble(self, store, input_path, **kwargs):
                captured.update(store=store, input_path=input_path, kwargs=kwargs)
                kwargs["progress"](0, 0, "layout status")
                return ["out.epub"]

        store = Store()
        with (
            patch("trans_novel.cli.common.load_config", return_value=config),
            patch("trans_novel.cli.common.runstore_for", return_value=store),
            patch("trans_novel.pipeline.Application", FakeOrchestrator),
        ):
            result = CliRunner().invoke(
                app,
                ["tools", "assemble", "input.epub", "--reanalyze-layout"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(captured["kwargs"]["reanalyze_layout"])
        self.assertIn("layout status", plain(result.output))

    def test_status_does_not_create_state_directory(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "novel.txt")
            state_dir = os.path.join(d, "state")
            with open(src, "w", encoding="utf-8") as f:
                f.write("第一段。\n")
            cfg = Config.from_dict({"llm": fake_llm_dict()})
            cfg.source_lang = "ja"
            cfg.state_dir = state_dir

            with patch("trans_novel.cli.common.load_config", return_value=cfg):
                result = CliRunner().invoke(app, ["status", src])

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("尚无进度", result.output)
            self.assertFalse(os.path.exists(state_dir))


class TestChapterProcessingOutput(unittest.TestCase):
    def test_prints_preserved_and_review_required_chapters(self):
        report = {
            "chapter_processing": {
                "preserved": [{"chapter": 2, "title": "Sources", "reason": "reference list"}],
                "review_required": [{"chapter": 3, "title": "Mixed", "reason": "uncertain"}],
            }
        }
        with patch("trans_novel.cli.common.console.print") as output:
            print_chapter_processing(report)

        rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
        self.assertIn("第2章 Sources", rendered)
        self.assertIn("第3章 Mixed", rendered)
        self.assertNotIn("--back-matter", rendered)


class TestWindowsConsoleEncoding(unittest.TestCase):
    class _Stream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    def test_configures_utf8_for_windows_streams(self):
        out = self._Stream()
        err = self._Stream()
        configure_windows_console((out, err), is_windows=True)

        self.assertEqual(out.calls, [{"encoding": "utf-8", "errors": "replace"}])
        self.assertEqual(err.calls, [{"encoding": "utf-8", "errors": "replace"}])

    def test_removed_qa_option_is_unknown(self):
        result = CliRunner().invoke(app, ["translate", "input.txt", "--qa"])
        self.assertEqual(result.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
