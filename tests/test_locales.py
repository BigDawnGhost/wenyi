"""CLI locale detection and catalog integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from string import Formatter

from trans_novel.locales import detect_ui_language
from trans_novel.locales.en import MESSAGES as EN_MESSAGES
from trans_novel.locales.zh import MESSAGES as ZH_MESSAGES


class TestLocaleDetection(unittest.TestCase):
    def test_english_is_the_default(self):
        self.assertEqual(
            detect_ui_language({}, is_windows=False),
            "en",
        )

    def test_explicit_override_has_highest_priority(self):
        self.assertEqual(
            detect_ui_language(
                {"WENYI_LANG": "zh-CN", "LC_ALL": "C.UTF-8"},
                is_windows=False,
            ),
            "zh",
        )
        self.assertEqual(
            detect_ui_language(
                {"WENYI_LANG": "en", "LC_ALL": "zh_CN.UTF-8"},
                is_windows=False,
            ),
            "en",
        )

    def test_standard_locale_variables_follow_precedence(self):
        self.assertEqual(
            detect_ui_language(
                {
                    "LC_ALL": "C.UTF-8",
                    "LC_MESSAGES": "zh_CN.UTF-8",
                    "LANG": "zh_CN.UTF-8",
                },
                is_windows=False,
            ),
            "en",
        )
        self.assertEqual(
            detect_ui_language(
                {"LC_MESSAGES": "zh_TW.UTF-8", "LANG": "en_US.UTF-8"},
                is_windows=False,
            ),
            "zh",
        )

    def test_windows_process_locale_is_used_without_environment_locale(self):
        self.assertEqual(
            detect_ui_language(
                {},
                is_windows=True,
                windows_locale="zh_CN",
            ),
            "zh",
        )

    def test_catalogs_have_the_same_keys(self):
        self.assertEqual(set(EN_MESSAGES), set(ZH_MESSAGES))
        for key in EN_MESSAGES:
            with self.subTest(key=key):
                english_fields = {
                    field
                    for _, field, _, _ in Formatter().parse(EN_MESSAGES[key])
                    if field
                }
                chinese_fields = {
                    field
                    for _, field, _, _ in Formatter().parse(ZH_MESSAGES[key])
                    if field
                }
                self.assertEqual(english_fields, chinese_fields)


class TestLocalizedCliHelp(unittest.TestCase):
    @staticmethod
    def _help(language: str, *arguments: str) -> str:
        project_root = str(Path(__file__).resolve().parents[1])
        env = os.environ.copy()
        env["WENYI_LANG"] = language
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (project_root, env.get("PYTHONPATH", "")) if item
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "trans_novel",
                    *arguments,
                    "--help",
                ],
                cwd=directory,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout

    def test_default_english_catalog_can_be_selected_explicitly(self):
        output = self._help("en")
        self.assertIn("A multilingual translation workflow", output)
        self.assertIn("Workflow", output)
        self.assertNotIn("主要流程", output)

    def test_chinese_catalog_can_be_selected_explicitly(self):
        output = self._help("zh_CN")
        self.assertIn("面向长篇小说的多语言翻译工作流", output)
        self.assertIn("主要流程", output)

    def test_command_and_option_help_use_the_selected_catalog(self):
        english = self._help("en", "translate")
        chinese = self._help("zh", "translate")

        self.assertIn("Book to translate", english)
        self.assertIn("Final format", english)
        self.assertIn("待翻译书籍", chinese)
        self.assertIn("最终导出格式", chinese)


if __name__ == "__main__":
    unittest.main()
