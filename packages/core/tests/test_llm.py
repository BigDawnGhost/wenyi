"""LLM 抽象层与 JSON 解析的测试（离线）。"""

from __future__ import annotations

import unittest

from wenyi_core.llm.base import FakeClient, parse_json_loose


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
    def test_fallback_chain(self):
        from wenyi_core.config import TierConfig
        from wenyi_core.llm.base import resolve_tier

        strong = TierConfig(model="pro")
        cheap = TierConfig(model="flash")
        fast = TierConfig(model="flash", thinking=False)

        # 三档全有 → 各归各
        tiers = {"strong": strong, "cheap": cheap, "fast": fast}
        self.assertIs(resolve_tier(tiers, "fast"), fast)
        self.assertIs(resolve_tier(tiers, "cheap"), cheap)
        self.assertIs(resolve_tier(tiers, "strong"), strong)
        # 无 fast → 落 cheap（不升到更贵的 strong）
        tiers2 = {"strong": strong, "cheap": cheap}
        self.assertIs(resolve_tier(tiers2, "fast"), cheap)
        # 只有 strong → 都落 strong
        tiers3 = {"strong": strong}
        self.assertIs(resolve_tier(tiers3, "fast"), strong)
        self.assertIs(resolve_tier(tiers3, "cheap"), strong)
        # 未知档 → 落 strong
        self.assertIs(resolve_tier(tiers, "unknown"), strong)


class TestFakeClient(unittest.TestCase):
    def test_default(self):
        c = FakeClient()
        self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "")
        self.assertEqual(c.complete_json([{"role": "user", "content": "x"}]), [])

    def test_handler(self):
        def handler(messages, tier, json_mode):
            return '["A","B"]' if json_mode else "hello"

        c = FakeClient(handler=handler)
        self.assertEqual(c.complete([{"role": "user", "content": "x"}]), "hello")
        self.assertEqual(c.complete_json([{"role": "user", "content": "x"}]), ["A", "B"])
        self.assertEqual(len(c.calls), 2)


class TestParseJsonLooseRepairs(unittest.TestCase):
    def test_inner_ascii_quotes_repaired(self):
        # 真实案例：claude-opus-4.6 经 OpenRouter 输出的译文含未转义英文引号
        raw = '{"translations":["磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。"]}'
        got = parse_json_loose(raw)
        self.assertEqual(got["translations"][0], '磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。')

    def test_trailing_extra_brace(self):
        # 真实案例：gemini-3.1-pro 输出末尾多一个 }
        self.assertEqual(parse_json_loose('{"a": 1}\n}'), {"a": 1})

    def test_unescaped_quotes_with_trailing_extra_brace_keeps_object(self):
        raw = '{"translations":["他说"好"。"]}\n}'
        self.assertEqual(
            parse_json_loose(raw),
            {"translations": ['他说"好"。']},
        )

    def test_valid_json_untouched(self):
        self.assertEqual(parse_json_loose('{"a": "b, c: d"}'), {"a": "b, c: d"})

    def test_escaped_quotes_still_work(self):
        self.assertEqual(parse_json_loose('{"a": "he said \\"hi\\""}'), {"a": 'he said "hi"'})


if __name__ == "__main__":
    unittest.main()
