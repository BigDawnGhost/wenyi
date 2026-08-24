"""SRT 字幕解析、并发翻译与 CLI 分流测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.ingest.srt_reader import parse_srt
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.srt_translation import translate_srt


def _sample_srt() -> str:
    return (
        "1\n00:00:01,000 --> 00:00:02,000\nHello world.\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nHow are you?\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nI am fine.\n\n"
    )


class TestSrtReader(unittest.TestCase):
    def test_parse_srt_basic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "demo.srt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_sample_srt())
            cues = parse_srt(path)
        self.assertEqual([cue.index for cue in cues], ["1", "2", "3"])
        self.assertEqual(cues[0].text, "Hello world.")


class TestSrtTranslate(unittest.TestCase):
    def test_translate_writes_output_and_state(self):
        def handler(messages, tier, json_mode):
            self.assertEqual(tier, "strong")
            user = messages[-1]["content"]
            # 批处理 JSON 模式
            if json_mode:
                payload = {}
                for index in ("1", "2", "3"):
                    if f'"{index}"' in user or f"'{index}'" in user or f"{index}" in user:
                        payload[index] = f"译{index}"
                # 简化：整批都回三条
                return json.dumps(
                    {"1": "你好世界。", "2": "你好吗？", "3": "我很好。"}, ensure_ascii=False
                )
            return "你好世界。"

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "demo.srt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_sample_srt())
            config = Config.from_dict(
                {
                    "paths": {"state_dir": os.path.join(directory, "state")},
                    "output": {"mono": True, "bilingual": True},
                    "llm": {"provider": "fake"},
                }
            )
            result = translate_srt(
                path,
                config,
                client=FakeClient(handler=handler),
            )
            self.assertEqual(result["cue_count"], 3)
            self.assertTrue(os.path.isdir(result["run_dir"]))
            self.assertTrue(any(p.endswith(".zh.srt") for p in result["outputs"]))
            self.assertTrue(any(p.endswith(".zh-bi.srt") for p in result["outputs"]))
            mono = next(p for p in result["outputs"] if p.endswith(".zh.srt"))
            with open(mono, encoding="utf-8") as handle:
                body = handle.read()
            self.assertIn("你好世界。", body)
            self.assertIn("00:00:01,000 --> 00:00:02,000", body)

    def test_cli_translate_routes_srt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "demo.srt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(_sample_srt())
            fake_result = {
                "translated": 3,
                "cue_count": 3,
                "run_dir": os.path.join(directory, "state", "srt", "demo"),
                "outputs": [os.path.join(directory, "output", "demo.zh.srt")],
                "usage": {},
            }
            with (
                patch("trans_novel.cli._load_config") as load_config,
                patch(
                    "trans_novel.pipeline.srt_translation.translate_srt",
                    return_value=fake_result,
                ) as translate_srt_mock,
            ):
                load_config.return_value = Config.from_dict({"llm": {"provider": "fake"}})
                result = CliRunner().invoke(app, ["translate", path])
            self.assertEqual(result.exit_code, 0, result.output)
            translate_srt_mock.assert_called_once()
            self.assertIn("字幕翻译完成", result.output)


if __name__ == "__main__":
    unittest.main()
