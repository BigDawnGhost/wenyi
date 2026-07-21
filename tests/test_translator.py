"""翻译 agent 的对齐保证测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import re
import unittest

from trans_novel.config import Config
from trans_novel.languages import get_bundle
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.locales import message as ui_message
from trans_novel.assemble.translator import Translator
from trans_novel.pipeline.checks import length_flags


def _count_segments(user_content: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", user_content, re.M))


class TestTranslatorAlignment(unittest.TestCase):
    def _config(self):
        return Config.from_dict({
            "language": {"source": "ja", "target": "zh"},
            "llm": {"provider": "fake", "tiers": {
                "strong": {"model": "deepseek-v4-pro"},
                "cheap": {"model": "deepseek-v4-flash"},
            }},
            "pipeline": {"align_retry_limit": 1},
        })

    def test_happy_path_aligned(self):
        def handler(messages, tier, json_mode):
            n = _count_segments(messages[-1]["content"])
            return json.dumps({"translations": [f"译{i}" for i in range(n)]},
                              ensure_ascii=False)

        t = Translator(FakeClient(handler=handler), self._config())
        out = t.translate_batch(["あ", "い", "う"])
        self.assertEqual(len(out), 3)
        self.assertEqual(out, ["译0", "译1", "译2"])

    def test_fallback_to_per_segment_on_mismatch(self):
        # 多段批次故意少返回一段；单段调用正常 → 触发逐段兜底
        def handler(messages, tier, json_mode):
            n = _count_segments(messages[-1]["content"])
            trans = [f"译{i}" for i in range(n)]
            if n > 1:
                trans = trans[:-1]  # 故意制造段数不符
            return json.dumps({"translations": trans}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        t = Translator(client, self._config())
        out = t.translate_batch(["あ", "い", "う"])
        self.assertEqual(len(out), 3)  # 兜底后仍保证 1:1
        # 验证确实回退到了逐段（出现过 n==1 的调用）
        single_calls = [c for c in client.calls
                        if _count_segments(c["messages"][-1]["content"]) == 1]
        self.assertGreaterEqual(len(single_calls), 3)

    def test_empty_per_segment_fallback_is_rejected(self):
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                {"translations": []}
            )
        )
        translator = Translator(client, self._config())

        with self.assertRaises(Exception) as raised:
            translator.translate_batch(["あ", "い"])
        self.assertEqual(
            str(raised.exception),
            ui_message("error.translation_fallback_failed", index=0),
        )

    def test_non_string_translation_is_rejected(self):
        client = FakeClient(
            handler=lambda messages, tier, json_mode: json.dumps(
                {"translations": [None]}
            )
        )
        translator = Translator(client, self._config())

        with self.assertRaises(Exception) as raised:
            translator.translate_batch(["あ"])
        self.assertEqual(
            str(raised.exception),
            ui_message("error.translation_fallback_failed", index=0),
        )


class TestTranslatorPromptOrder(unittest.TestCase):
    def test_static_chapter_digest_precedes_dynamic_glossary(self):
        for target, digest, glossary in (
            ("zh", "【本章梗概】", "【专有名词对照表】"),
            ("en", "[Chapter digest]", "[Proper-noun glossary"),
        ):
            with self.subTest(target=target):
                bundle = get_bundle(target)
                for name in ("translator_user", "translator_fix_user"):
                    template = bundle.templates[name].template
                    self.assertLess(template.index(digest), template.index(glossary))


class TestChecks(unittest.TestCase):
    def test_length_flags(self):
        sources = ["これは長い日本語の文章です。" * 3, "短い", "x" * 10]
        targets = ["", "短い但正常的中文译文内容", "x" * 40]
        flags = length_flags(sources, targets)
        kinds = {f.index: f.reason for f in flags}
        self.assertEqual(kinds.get(0), "empty")     # 译文为空
        self.assertEqual(kinds.get(2), "too_long")  # 比值过大

    def test_cjk_to_english_uses_language_aware_length_limit(self):
        flags = length_flags(
            ["先生。", "雪国"],
            ["Professor.", "Snow Country"],
            source_lang="ja",
            target_lang="en",
        )

        self.assertEqual(flags, [])


if __name__ == "__main__":
    unittest.main()
