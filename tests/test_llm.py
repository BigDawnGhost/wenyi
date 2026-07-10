"""LLM 抽象层与 JSON 解析的测试（离线）。"""

from __future__ import annotations

import unittest

from trans_novel.llm.base import FakeClient, parse_json_loose


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
        from trans_novel.config import TierConfig
        from trans_novel.llm.base import resolve_tier

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


if __name__ == "__main__":
    unittest.main()


class TestBuildRequestKwargs(unittest.TestCase):
    def _cfg(self, **kw):
        from trans_novel.config import LLMConfig
        return LLMConfig(**kw)

    def _tier(self, **kw):
        from trans_novel.config import TierConfig
        return TierConfig(model="m", **kw)

    def _build(self, cfg, tcfg, **kw):
        from trans_novel.llm.base import build_request_kwargs
        return build_request_kwargs(cfg, tcfg, [{"role": "user", "content": "x"}], **kw)

    def test_deepseek_style_unchanged(self):
        k = self._build(self._cfg(), self._tier(thinking=True, reasoning_effort="high"))
        self.assertEqual(k["reasoning_effort"], "high")
        self.assertEqual(k["extra_body"], {"thinking": {"type": "enabled"}})

    def test_openrouter_auto_by_base_url(self):
        cfg = self._cfg(base_url="https://openrouter.ai/api/v1")
        k = self._build(cfg, self._tier(thinking=True, reasoning_effort="high"))
        self.assertNotIn("reasoning_effort", k)
        self.assertEqual(k["extra_body"], {"reasoning": {"effort": "high"}})
        k2 = self._build(cfg, self._tier(thinking=False))
        self.assertEqual(k2["extra_body"], {"reasoning": {"enabled": False}})

    def test_openai_style(self):
        cfg = self._cfg(provider="openai", base_url="https://api.openai.com/v1")
        k = self._build(cfg, self._tier(thinking=True, reasoning_effort="low"))
        self.assertEqual(k["reasoning_effort"], "low")
        self.assertNotIn("extra_body", k)

    def test_none_style_sends_nothing(self):
        cfg = self._cfg(provider="openai", reasoning_style="none")
        k = self._build(cfg, self._tier(thinking=True))
        self.assertNotIn("reasoning_effort", k)
        self.assertNotIn("extra_body", k)

    def test_explicit_style_overrides_auto(self):
        cfg = self._cfg(base_url="https://openrouter.ai/api/v1", reasoning_style="deepseek")
        k = self._build(cfg, self._tier(thinking=True))
        self.assertIn("reasoning_effort", k)

    def test_tier_extra_body_merges_and_overrides(self):
        cfg = self._cfg(provider="openai", reasoning_style="none")
        t = self._tier(thinking=True, extra_body={"enable_thinking": True})
        k = self._build(cfg, t)
        self.assertEqual(k["extra_body"], {"enable_thinking": True})

    def test_max_tokens_floor_with_thinking(self):
        k = self._build(self._cfg(), self._tier(thinking=True), max_tokens=100)
        self.assertEqual(k["max_tokens"], 4096)
        k2 = self._build(self._cfg(), self._tier(thinking=False), max_tokens=100)
        self.assertEqual(k2["max_tokens"], 100)

    def test_deepseek_client_alias(self):
        from trans_novel.llm.base import DeepSeekClient, OpenAICompatClient
        self.assertIs(DeepSeekClient, OpenAICompatClient)

    def test_build_client_accepts_openai_provider(self):
        from trans_novel.config import Config
        from trans_novel.llm.base import OpenAICompatClient, build_client
        cfg = Config.from_dict({"llm": {"provider": "openai", "tiers": {"strong": {"model": "m"}}}})
        self.assertIsInstance(build_client(cfg), OpenAICompatClient)
