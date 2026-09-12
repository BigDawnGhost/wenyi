"""翻译 agent 的对齐保证测试（离线 FakeClient）。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from types import SimpleNamespace

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.agents.translator import AlignmentError, Translator
from trans_novel.config import Config, ModelRef
from trans_novel.glossary.store import GlossaryTerm
from trans_novel.ingest.models import KIND_HEADING, KIND_TEXT, Segment
from trans_novel.llm import FakeClient
from trans_novel.llm.errors import AllModelsFailedError
from trans_novel.pipeline.nodes import translate_batch
from trans_novel.pipeline.quality import count_aligned, length_flags
from trans_novel.pipeline.state import RollingContext


def _count_segments(user_content: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", user_content, re.M))


class TestTranslatorImportSurface(unittest.TestCase):
    def test_public_translator_import_does_not_cycle_in_clean_process(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from trans_novel.agents.translator import Translator",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TestTranslatorPlainContract(unittest.TestCase):
    def _config(self):
        config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
        config.source_lang = "en"
        config.pipeline.protocol_retry_limit = 1
        return config

    def test_translation_and_editor_repair_contract(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: (
                json.dumps({"translations": ["甲乙"]}, ensure_ascii=False)
                if json_mode
                else "改甲乙"
            )
        )
        translator = Translator(client, self._config())

        result = translator.translate_batch(["Alpha Beta"], agent="translator")
        self.assertEqual(result.translations, ("甲乙",))
        self.assertEqual(result.request_count, 1)
        self.assertEqual(
            translator.repair_issue(
                "Alpha Beta",
                "甲乙",
                issue_type="number_mismatch",
                issue_detail="数字缺失",
            ),
            "改甲乙",
        )
        repair_call = client.calls[-1]
        self.assertEqual(repair_call["agent"], "editor")
        self.assertEqual(repair_call["operation"], "translate.repair")
        self.assertNotIn("EPUB", client.calls[0]["messages"][-1]["content"])

    def test_plain_response_removes_forbidden_controls_but_preserves_text(self):
        client = FakeClient(handler=lambda *args: "前\x02后\t行\n下一\r终😀")
        translator = Translator(client, self._config())

        self.assertEqual(
            translator.repair_issue("Alpha", "旧译文", issue_type="style", issue_detail="格式"),
            "前后\t行\n下一\r终😀",
        )

    def test_page_literals_preserve_source_without_call(self):
        client = FakeClient(handler=lambda *args: self.fail("LLM must not be called"))
        sources = ["123", "258–59", "xix", "xxvi", "xix–xx"]
        result = Translator(client, self._config()).translate_batch(sources, agent="translator")
        self.assertEqual(result.translations, tuple(sources))
        self.assertEqual(result.request_count, 0)
        self.assertEqual(client.calls, [])


class TestTranslatorAlignment(unittest.TestCase):
    def _config(self):
        config = Config.from_dict({"llm": fake_llm_dict(), "quality": "economy"})
        config.source_lang = "ja"
        config.pipeline.protocol_retry_limit = 1
        return config

    def test_happy_path_aligned(self):
        def handler(messages, agent, operation, json_mode):
            n = _count_segments(messages[-1]["content"])
            return json.dumps({"translations": [f"译{i}" for i in range(n)]}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        t = Translator(client, self._config())
        result = t.translate_batch(["あ", "い", "う"], agent="translator")
        self.assertEqual(len(result.translations), 3)
        self.assertEqual(result.translations, ("译0", "译1", "译2"))
        self.assertEqual(result.request_count, 1)
        self.assertTrue(all(c["agent"] == "translator" for c in client.calls))
        self.assertTrue(all(c["operation"] == "translate.batch" for c in client.calls))

    def test_json_response_removes_escaped_forbidden_control(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: json.dumps(
                {"translations": ["前\x02后"]}, ensure_ascii=False
            )
        )
        result = Translator(client, self._config()).translate_batch(["Alpha"], agent="translator")
        self.assertEqual(result.translations, ("前后",))

    def test_all_forbidden_model_output_uses_retry_and_fallback_rejection(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: (
                json.dumps({"translations": ["\x02"]}) if json_mode else "\x02"
            )
        )
        translator = Translator(client, self._config())

        with self.assertRaises(AlignmentError):
            translator.translate_batch(["Alpha"], agent="translator")
        self.assertEqual(len(client.calls), 4)

    def test_non_linguistic_segments_are_preserved_and_excluded_from_prompt(self):
        def handler(messages, agent, operation, json_mode):
            self.assertEqual(_count_segments(messages[-1]["content"]), 1)
            return json.dumps({"translations": ["你好"]}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        translator = Translator(client, self._config())

        translated = translator.translate_batch(["-", "123", "Hello"], agent="translator")
        untouched = translator.translate_batch(["—", "2026"], agent="translator")

        self.assertEqual(translated.translations, ("-", "123", "你好"))
        self.assertEqual(untouched.translations, ("—", "2026"))
        self.assertEqual(translated.request_count, 1)
        self.assertEqual(untouched.request_count, 0)
        self.assertEqual(len(client.calls), 1)

    def test_fallback_to_per_segment_on_mismatch(self):
        # 多段批次故意少返回一段；单段调用按源段返回不同译文 → 触发逐段兜底
        def handler(messages, agent, operation, json_mode):
            if not json_mode:
                source = messages[-1]["content"].rsplit("】", 1)[-1].strip()
                return {"あ": "译0", "い": "译1", "う": "译2"}[source]
            n = _count_segments(messages[-1]["content"])
            trans = [f"译{i}" for i in range(n)]
            if n > 1:
                trans = trans[:-1]  # 故意制造段数不符
            return json.dumps({"translations": trans}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        t = Translator(client, self._config())
        result = t.translate_batch(["あ", "い", "う"], agent="translator")
        self.assertEqual(len(result.translations), 3)  # 兜底后仍保证 1:1
        self.assertEqual(result.translations, ("译0", "译1", "译2"))
        self.assertEqual(result.request_count, 5)
        single_calls = [call for call in client.calls if not call["json_mode"]]
        self.assertEqual(len(single_calls), 3)

    def test_empty_per_segment_fallback_is_rejected(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: (
                json.dumps({"translations": []}) if json_mode else ""
            )
        )
        translator = Translator(client, self._config())

        with self.assertRaisesRegex(Exception, "索引为 0 的段落"):
            translator.translate_batch(["あ", "い"], agent="translator")

    def test_abnormally_long_per_segment_fallback_is_rejected(self):
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: (
                json.dumps({"translations": [None]}) if json_mode else "译" * 300
            )
        )
        translator = Translator(client, self._config())

        with self.assertRaisesRegex(Exception, "索引为 0 的段落"):
            translator.translate_batch(["あ"], agent="translator")

    def test_malformed_json_recovers_via_per_segment_fallback(self):
        """整批翻译返回无法解析的内容 → JSONParseError 触发整批重试，再由逐段兜底恢复一一对应。"""

        def handler(messages, agent, operation, json_mode):
            if not json_mode:
                return "译0"
            n = _count_segments(messages[-1]["content"])
            if n > 1:
                return "模型输出了不可解析的内容"
            return json.dumps({"translations": [f"译{i}" for i in range(n)]}, ensure_ascii=False)

        client = FakeClient(handler=handler)
        translator = Translator(client, self._config())
        result = translator.translate_batch(["あ", "い"], agent="translator")
        self.assertEqual(result.translations, ("译0", "译0"))
        self.assertEqual(result.request_count, 4)
        # protocol_retry_limit=1 → 2 次整批 + 逐段 2 次，全部计入逻辑调用
        self.assertEqual(len(client.calls), 4)

    def test_malformed_json_in_fallback_raises_alignment_with_stable_reason(self):
        """逐段兜底时仍返回无法解析的内容 → 包装为 AlignmentError，提供稳定 reason 并保留中文消息。"""
        client = FakeClient(
            handler=lambda messages, agent, operation, json_mode: "不可解析" if json_mode else ""
        )
        translator = Translator(client, self._config())
        with self.assertRaises(AlignmentError) as caught:
            translator.translate_batch(["あ"], agent="translator")
        self.assertEqual(caught.exception.reason, "translation_segment_fallback_failed")
        self.assertRegex(str(caught.exception), "索引为 0 的段落")
        self.assertEqual(len(client.calls), 4)  # 2 次整批 + 2 次单段兜底

    def test_provider_permanent_error_bubbles_immediately(self):
        provider_error = RuntimeError("provider permanent")

        def handler(messages, agent, operation, json_mode):
            raise provider_error

        client = FakeClient(handler=handler)
        translator = Translator(client, self._config())
        with self.assertRaises(RuntimeError) as caught:
            translator.translate_batch(["あ", "い"], agent="translator")
        self.assertIs(caught.exception, provider_error, "必须原样向上抛出异常对象")
        self.assertEqual(len(client.calls), 1, "Provider 异常不得触发整批重试")

    def test_provider_retryable_error_bubbles_immediately(self):
        provider_error = TimeoutError("provider timeout")

        def handler(messages, agent, operation, json_mode):
            raise provider_error

        client = FakeClient(handler=handler)
        translator = Translator(client, self._config())
        with self.assertRaises(TimeoutError) as caught:
            translator.translate_batch(["あ", "い"], agent="translator")
        self.assertIs(caught.exception, provider_error, "必须原样向上抛出异常对象")
        self.assertEqual(len(client.calls), 1)

    def test_retry_exhaustion_bubbles_immediately(self):
        provider_error = AllModelsFailedError(((ModelRef("provider", "model"), "server_error"),))

        def handler(messages, agent, operation, json_mode):
            raise provider_error

        client = FakeClient(handler=handler)
        translator = Translator(client, self._config())
        with self.assertRaises(AllModelsFailedError) as caught:
            translator.translate_batch(["あ", "い"], agent="translator")
        self.assertIs(caught.exception, provider_error, "必须原样向上抛出异常对象")
        self.assertEqual(len(client.calls), 1)

    def test_ordinary_value_error_bubbles_immediately(self):
        business_error = ValueError("业务拒绝")

        def handler(messages, agent, operation, json_mode):
            raise business_error

        client = FakeClient(handler=handler)
        translator = Translator(client, self._config())
        with self.assertRaises(ValueError) as caught:
            translator.translate_batch(["あ", "い"], agent="translator")
        self.assertIs(caught.exception, business_error, "业务错误不得被吞掉，也不得触发重试")
        self.assertEqual(len(client.calls), 1)

    def test_provider_failure_during_fallback_bubbles_immediately(self):
        provider_error = AllModelsFailedError(((ModelRef("provider", "model"), "server_error"),))

        def handler(messages, agent, operation, json_mode):
            n = _count_segments(messages[-1]["content"])
            if n > 1:
                # 整批翻译故意少返回一段译文 → AlignmentError 触发整批重试和逐段兜底
                return json.dumps(
                    {"translations": [f"译{i}" for i in range(n - 1)]}, ensure_ascii=False
                )
            raise provider_error  # 第一个单段兜底调用：Provider 失败

        client = FakeClient(handler=handler)
        translator = Translator(client, self._config())
        with self.assertRaises(AllModelsFailedError) as caught:
            translator.translate_batch(["あ", "い", "う"], agent="translator")
        self.assertIs(caught.exception, provider_error, "兜底阶段的 Provider 异常必须原样向上抛出")
        # 2 次整批（少一段）+ 第 1 个单段调用即失败，不再继续兜底
        self.assertEqual(len(client.calls), 3)


class TestTranslatorSingleSegmentContract(unittest.TestCase):
    def _config(self):
        config = Config.from_dict({"llm": fake_llm_dict(), "quality": "balanced"})
        config.source_lang = "en"
        config.pipeline.protocol_retry_limit = 1
        return config

    def test_each_source_has_its_own_plain_text_call(self):
        def handler(messages, agent, operation, json_mode):
            user = messages[-1]["content"]
            self.assertFalse(json_mode)
            self.assertNotEqual("Alpha" in user, "Beta" in user)
            return "甲" if "Alpha" in user else "乙"

        client = FakeClient(handler=handler)
        result = Translator(client, self._config()).translate_batch(
            ["Alpha", "Beta", "123", "{var=dc:http_errors:rate10m,job=webserver}"],
            agent="translator",
        )

        self.assertEqual(
            result.translations,
            ("甲", "乙", "123", "{var=dc:http_errors:rate10m,job=webserver}"),
        )
        self.assertEqual(result.request_count, 2)

    def test_invalid_plain_text_retries(self):
        responses = iter(["", "译" * 300, "正确译文"])
        config = self._config()
        config.pipeline.protocol_retry_limit = 2
        client = FakeClient(handler=lambda messages, agent, operation, json_mode: next(responses))

        result = Translator(client, config).translate_batch(["Alpha"], agent="translator")
        self.assertEqual(result.translations, ("正确译文",))
        self.assertEqual(result.request_count, 3)
        self.assertEqual(len(client.calls), 3)

    def test_contract_exhaustion_fails_closed(self):
        client = FakeClient(handler=lambda messages, agent, operation, json_mode: "")

        with self.assertRaises(AlignmentError) as caught:
            Translator(client, self._config()).translate_batch(["Alpha"], agent="translator")

        self.assertEqual(caught.exception.reason, "translation_segment_contract_failed")
        self.assertEqual(len(client.calls), 2)

    def test_overlong_primary_falls_back_to_analyst(self):
        calls = []

        def handler(messages, agent, operation, json_mode):
            calls.append((agent, operation))
            return "译" * 300 if agent == "translator" else "分析译文"

        client = FakeClient(handler=handler)
        result = Translator(client, self._config()).translate_batch(
            ["Alpha"],
            agent="translator",
            operation="translate.single",
            fallback_agent="analyst",
        )

        self.assertEqual(result.translations, ("分析译文",))
        self.assertEqual(result.request_count, 3)
        self.assertEqual(
            calls,
            [
                ("translator", "translate.single"),
                ("translator", "translate.single"),
                ("analyst", "translate.single"),
            ],
        )
        self.assertEqual(len(client.calls), 3)

    def test_primary_and_analyst_exhaustion_fails_without_partial_result(self):
        calls = []

        def handler(messages, agent, operation, json_mode):
            calls.append((agent, operation))
            return "译" * 300

        translator = Translator(FakeClient(handler=handler), self._config())
        with self.assertRaises(AlignmentError) as caught:
            translator.translate_batch(
                ["Alpha"],
                agent="translator",
                operation="translate.single",
                fallback_agent="analyst",
            )

        self.assertEqual(caught.exception.reason, "translation_segment_contract_failed")
        self.assertEqual(
            calls,
            [
                ("translator", "translate.single"),
                ("translator", "translate.single"),
                ("analyst", "translate.single"),
                ("analyst", "translate.single"),
            ],
        )

    def test_provider_exception_does_not_invoke_business_fallback(self):
        provider_error = RuntimeError("provider failed")
        calls = []

        def handler(messages, agent, operation, json_mode):
            calls.append((agent, operation))
            raise provider_error

        translator = Translator(FakeClient(handler=handler), self._config())
        with self.assertRaises(RuntimeError) as caught:
            translator.translate_batch(
                ["Alpha"],
                agent="translator",
                operation="translate.single",
                fallback_agent="analyst",
            )

        self.assertIs(caught.exception, provider_error)
        self.assertEqual(calls, [("translator", "translate.single")])


class TestTranslateNodeHeadingPrompt(unittest.TestCase):
    def _node(self, client):
        config = Config.from_dict({"llm": fake_llm_dict(), "quality": "balanced"})
        config.source_lang = "en"
        config.pipeline.protocol_retry_limit = 1
        translator = Translator(client, config)
        return SimpleNamespace(translator=translator, config=config)

    def test_heading_uses_concise_prompt_and_matching_terms_only(self):
        users = []

        def handler(messages, agent, operation, json_mode):
            self.assertFalse(json_mode)
            self.assertEqual(agent, "analyst" if operation == "translate.heading" else "translator")
            users.append((operation, messages[-1]["content"]))
            user = messages[-1]["content"]
            if operation == "translate.heading":
                self.assertNotIn("STYLE_MARKER", user)
                self.assertNotIn("CONTEXT_MARKER", user)
                if "RED SUPERGIANT" in user:
                    self.assertIn("红超巨星", user)
                    self.assertNotIn("黑洞", user)
                    return "红超巨星"
                return "目录"
            else:
                self.assertIn("STYLE_MARKER", user)
                self.assertIn("CONTEXT_MARKER", user)
                return "正文译文"

        client = FakeClient(handler=handler)
        node = self._node(client)
        terms = [
            GlossaryTerm(source="RED SUPERGIANT", target="红超巨星"),
            GlossaryTerm(source="BLACK HOLE", target="黑洞"),
        ]
        segments = [
            Segment(index=0, source="Contents", kind=KIND_HEADING),
            Segment(index=1, source="RED SUPERGIANT", kind=KIND_HEADING),
            Segment(index=2, source="A paragraph.", kind=KIND_TEXT),
        ]
        translated = translate_batch(
            node.translator,
            segments,
            terms,
            RollingContext(recent_targets=["CONTEXT_MARKER"]),
            "STYLE_MARKER",
            chapter_segments=segments,
            start_index=0,
            chapter_title="Chapter",
            n_recent=6,
        )

        self.assertEqual(translated[0], ["目录", "红超巨星", "正文译文"])
        self.assertEqual(translated[1], 3)
        self.assertEqual(
            [operation for operation, _ in users],
            ["translate.heading", "translate.heading", "translate.single"],
        )

    def test_heading_keeps_strict_length_rejection_and_retries(self):
        client = FakeClient(handler=lambda messages, agent, operation, json_mode: "译" * 300)
        node = self._node(client)
        segments = [Segment(index=0, source="Contents", kind=KIND_HEADING)]

        translated = translate_batch(
            node.translator,
            segments,
            [],
            RollingContext(recent_targets=["CONTEXT_MARKER"]),
            "STYLE_MARKER",
            chapter_segments=segments,
            start_index=0,
            chapter_title="Chapter",
            n_recent=6,
        )
        self.assertEqual(translated, (["Contents"], 0))
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["operation"] == "translate.heading" for call in client.calls))
        self.assertTrue(all(call["agent"] == "analyst" for call in client.calls))

    def test_prose_falls_back_to_analyst_after_translator_exhaustion(self):
        def handler(messages, agent, operation, json_mode):
            if agent == "translator":
                return "译" * 300
            return "分析译文"

        client = FakeClient(handler=handler)
        node = self._node(client)
        segments = [Segment(index=0, source="A paragraph.", kind=KIND_TEXT)]
        translated = translate_batch(
            node.translator,
            segments,
            [],
            RollingContext(recent_targets=["CONTEXT_MARKER"]),
            "STYLE_MARKER",
            chapter_segments=segments,
            start_index=0,
            chapter_title="Chapter",
            n_recent=6,
        )
        self.assertEqual(translated[0], ["分析译文"])
        self.assertEqual(translated[1], 3)
        self.assertEqual(
            [(call["agent"], call["operation"]) for call in client.calls],
            [
                ("translator", "translate.single"),
                ("translator", "translate.single"),
                ("analyst", "translate.single"),
            ],
        )

    def test_generic_style_prompt_still_rejects_overlong_output(self):
        def handler(messages, agent, operation, json_mode):
            return "译" * 300 if "STYLE_MARKER" in messages[-1]["content"] else "目录"

        client = FakeClient(handler=handler)
        translator = Translator(client, self._node(client).config)

        with self.assertRaises(AlignmentError):
            translator.translate_batch(
                ["Contents"],
                agent="translator",
                operation="translate.single",
                style="STYLE_MARKER",
            )


class TestChecks(unittest.TestCase):
    def test_count_aligned(self):
        self.assertTrue(count_aligned(["a", "b"], ["甲", "乙"]))
        self.assertFalse(count_aligned(["a", "b"], ["甲"]))

    def test_length_flags(self):
        sources = ["これは長い日本語の文章です。" * 3, "短い", "x" * 10]
        targets = ["", "短い但正常的中文译文内容", "x" * 40]
        flags = length_flags(sources, targets)
        kinds = {f.index: f.reason for f in flags}
        self.assertEqual(kinds.get(0), "empty")  # 译文为空
        self.assertEqual(kinds.get(2), "too_long")  # 比值过大


if __name__ == "__main__":
    unittest.main()
