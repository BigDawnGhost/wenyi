"""LLM 抽象层、工厂与 JSON 解析的离线测试。"""

from __future__ import annotations

import unittest

from trans_novel.llm.json_parser import parse_json_loose, parse_json_result
from trans_novel.llm.providers.fake import FakeClient


class TestParseJsonLoose(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_loose('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(parse_json_loose("```json\n[1,2,3]\n```"), [1, 2, 3])

    def test_surrounded_by_prose(self):
        text = '思考结束。结果如下：["译文1","译文2"] 完毕。'
        self.assertEqual(parse_json_loose(text), ["译文1", "译文2"])

    def test_failure(self):
        with self.assertRaises(ValueError):
            parse_json_loose("没有任何 JSON 内容")


class TestResolveTier(unittest.TestCase):
    def test_only_exact_known_tiers_are_resolved(self):
        from trans_novel.config import TierConfig
        from trans_novel.llm.tiers import resolve_tier

        strong = TierConfig(model="pro")
        cheap = TierConfig(model="flash")
        fast = TierConfig(model="flash", thinking=False)
        tiers = {"strong": strong, "cheap": cheap, "fast": fast}
        self.assertIs(resolve_tier(tiers, "fast"), fast)
        self.assertIs(resolve_tier(tiers, "cheap"), cheap)
        self.assertIs(resolve_tier(tiers, "strong"), strong)
        with self.assertRaisesRegex(KeyError, "未配置 LLM tier：fast"):
            resolve_tier({"strong": strong, "cheap": cheap}, "fast")
        with self.assertRaisesRegex(ValueError, "未知 LLM tier：unknown"):
            resolve_tier(tiers, "unknown")


class TestFakeClient(unittest.TestCase):
    def test_default(self):
        client = FakeClient()
        self.assertEqual(client.complete([{"role": "user", "content": "x"}]), "")
        self.assertEqual(client.complete_json([{"role": "user", "content": "x"}]), [])

    def test_handler(self):
        def handler(messages, tier, json_mode):
            return '["A","B"]' if json_mode else "hello"

        client = FakeClient(handler=handler)
        self.assertEqual(client.complete([{"role": "user", "content": "x"}]), "hello")
        self.assertEqual(
            client.complete_json([{"role": "user", "content": "x"}]),
            ["A", "B"],
        )
        self.assertEqual(len(client.calls), 2)

    def test_unknown_tier_is_rejected_before_recording_a_call(self):
        client = FakeClient()

        with self.assertRaisesRegex(ValueError, "未知 LLM tier：typo"):
            client.complete([{"role": "user", "content": "x"}], tier="typo")

        self.assertEqual(client.calls, [])


class TestParseJsonLooseRepairs(unittest.TestCase):
    def test_parse_result_reports_whether_repair_was_used(self):
        self.assertFalse(parse_json_result('{"a": 1}').repaired)
        repaired = parse_json_result('{"a": 1')
        self.assertTrue(repaired.repaired)
        self.assertEqual(repaired.value, {"a": 1})

    def test_inner_ascii_quotes_repaired(self):
        raw = '{"translations":["磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。"]}'
        got = parse_json_loose(raw)
        self.assertEqual(
            got["translations"][0],
            '磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。',
        )

    def test_trailing_extra_brace(self):
        self.assertEqual(parse_json_loose('{"a": 1}\n}'), {"a": 1})

    def test_unescaped_quotes_with_trailing_extra_brace_keeps_object(self):
        raw = '{"translations":["他说"好"。"]}\n}'
        self.assertEqual(parse_json_loose(raw), {"translations": ['他说"好"。']})

    def test_valid_json_untouched(self):
        self.assertEqual(parse_json_loose('{"a": "b, c: d"}'), {"a": "b, c: d"})

    def test_escaped_quotes_still_work(self):
        self.assertEqual(
            parse_json_loose('{"a": "he said \\"hi\\""}'),
            {"a": 'he said "hi"'},
        )


class TestProviderFactory(unittest.TestCase):
    @staticmethod
    def _real_config(api_format: str):
        from trans_novel.config import Config

        return Config.from_dict(
            {
                "llm": {
                    "api_format": api_format,
                    "api_key": "secret",
                    "base_url": "https://example.test/v1",
                    "model": "m",
                }
            }
        )

    def test_real_formats_share_universal_client(self):
        from trans_novel.llm.factory import build_client
        from trans_novel.llm.providers.universal import UniversalClient

        for api_format in ("openai", "anthropic"):
            with self.subTest(api_format=api_format):
                client = build_client(self._real_config(api_format))
                self.assertIsInstance(client, UniversalClient)
                self.assertEqual(client.api_format, api_format)

    def test_fake_format_keeps_offline_client(self):
        from trans_novel.config import Config
        from trans_novel.llm.factory import build_client

        self.assertIsInstance(
            build_client(Config.from_dict({"llm": {"api_format": "fake"}})),
            FakeClient,
        )

    def test_real_client_reports_all_missing_runtime_fields(self):
        from trans_novel.config import Config
        from trans_novel.llm.factory import build_client

        with self.assertRaisesRegex(ValueError, "api_key.*base_url.*model"):
            build_client(Config.from_dict({"llm": {"api_format": "openai"}}))


if __name__ == "__main__":
    unittest.main()
