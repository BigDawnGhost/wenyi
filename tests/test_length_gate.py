"""首译长度门 测试（离线）：过短段定向重译 / 保守保留 / 润色回退 / 过长只上报。

fake 译文不再与现实模型同分布（恒定长度），因此本文件全部使用
测试内联 handler 精确控制每次调用的输出长度：
批翻译先返回"半截漏译"式的过短段，门的单段重译再补足。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.fake_llm import _numbered_segments, routing_handler
from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator

_SEG0 = "あ" * 30
_SEG1 = "い" * 30
_LONG0 = "补足后足够长的译文文本" * 2  # 30 字符，与源段等长，过门
_LONG = "足够长的译文文本" * 3  # 24 字符，过门
_SHORT = "短"  # 1 字符，必被标记


def _txt(path: str) -> None:
    # 不写章节标题行，整篇是一章且只有两段正文，段索引恰为 0/1
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{_SEG0}\n\n{_SEG1}\n")


def _config(state: str, **pipeline) -> Config:
    base = {"polish": False, "book_understanding": False}
    base.update(pipeline)
    return Config.from_dict(
        {
            "language": {"source": "ja", "target": "zh"},
            "llm": {
                "provider": "fake",
                "tiers": {
                    "strong": {"model": "p"},
                    "cheap": {"model": "f"},
                    "fast": {"model": "f"},
                },
            },
            "pipeline": base,
            "paths": {"state_dir": state},
        }
    )


def _translation_calls(client: FakeClient) -> list:
    return [
        c for c in client.calls if "文学翻译" in c["messages"][0]["content"]
    ]


def _batch_events(store) -> list[dict]:
    with open(store.event_log_path, "r", encoding="utf-8") as f:
        return [
            json.loads(line)
            for line in f
            if line.strip() and json.loads(line)["event"] == "batch_translated"
        ]


class TestTranslateLengthGate(unittest.TestCase):
    def test_flagged_segment_is_retranslated_and_adopted(self):
        """批内短段 → 单段重译补足 → 采纳；事件记录 retranslated。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            _txt(txt)

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                user = messages[-1]["content"]
                if "文学翻译" in system:
                    segs = _numbered_segments(user)
                    if len(segs) == 1:
                        # 门的定向重译：给足长度
                        return json.dumps({"translations": [_LONG0]}, ensure_ascii=False)
                    # 批翻译：第 0 段"半截漏译"，其余正常
                    return json.dumps(
                        {"translations": [_SHORT] + [_LONG] * (len(segs) - 1)},
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            store = Orchestrator(_config(os.path.join(d, "state")), client=client).run(txt)
            chapter = store.load_chapter(0)

            self.assertEqual(chapter.text_segments[0].target, _LONG0)
            self.assertEqual(chapter.text_segments[1].target, _LONG)
            # 1 次整批 + 1 次门重译，不多不少
            self.assertEqual(len(_translation_calls(client)), 2)
            events = _batch_events(store)
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["length_fixes"],
                [{"index": 0, "reason": "too_short", "action": "retranslated"}],
            )

    def test_retry_still_short_keeps_original_without_loop(self):
        """重译仍短 → 保守保留原译、只重试一次，事件记录 retry_still_flagged。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            _txt(txt)

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                user = messages[-1]["content"]
                if "文学翻译" in system:
                    segs = _numbered_segments(user)
                    if len(segs) == 1:
                        return json.dumps({"translations": ["仍短"]}, ensure_ascii=False)
                    return json.dumps(
                        {"translations": [_SHORT] + [_LONG] * (len(segs) - 1)},
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            store = Orchestrator(_config(os.path.join(d, "state")), client=client).run(txt)
            chapter = store.load_chapter(0)

            self.assertEqual(chapter.text_segments[0].target, _SHORT)  # 保留原译
            self.assertEqual(len(_translation_calls(client)), 2)  # 1 批 + 恰好 1 次重试
            self.assertEqual(
                _batch_events(store)[0]["length_fixes"],
                [{"index": 0, "reason": "too_short", "action": "retry_still_flagged"}],
            )

    def test_gate_disabled_keeps_short_output_silently(self):
        """translate_length_gate=false：短译文原样保留，零额外调用，无事件记录。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            _txt(txt)

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                user = messages[-1]["content"]
                if "文学翻译" in system:
                    segs = _numbered_segments(user)
                    return json.dumps(
                        {"translations": [_SHORT] + [_LONG] * (len(segs) - 1)},
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            cfg = _config(os.path.join(d, "state"), translate_length_gate=False)
            store = Orchestrator(cfg, client=client).run(txt)
            chapter = store.load_chapter(0)

            self.assertEqual(chapter.text_segments[0].target, _SHORT)
            self.assertEqual(len(_translation_calls(client)), 1)
            self.assertEqual(_batch_events(store)[0]["length_fixes"], [])

    def test_polish_shortening_reverts_to_prepolish(self):
        """润色使某段异常变短 → 零成本回退润色前译文，不发额外 LLM 调用。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            _txt(txt)

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                user = messages[-1]["content"]
                if "文学翻译" in system:
                    segs = _numbered_segments(user)
                    # 翻译本身长度正常（门不触发）
                    return json.dumps(
                        {"translations": [_LONG] * len(segs)}, ensure_ascii=False
                    )
                if "文学润色编辑" in system:
                    segs = _numbered_segments(user)
                    # 润色把第 0 段改得异常短
                    return json.dumps(
                        {"polished": ["润"] + [_LONG + "润"] * (len(segs) - 1)},
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            cfg = _config(os.path.join(d, "state"), polish=True)
            store = Orchestrator(cfg, client=client).run(txt)
            chapter = store.load_chapter(0)

            self.assertEqual(chapter.text_segments[0].target, _LONG)  # 回退润色前
            self.assertEqual(chapter.text_segments[1].target, _LONG + "润")  # 其余正常润色
            self.assertEqual(
                _batch_events(store)[0]["length_fixes"],
                [{"index": 0, "reason": "too_short", "action": "polish_reverted"}],
            )

    def test_too_long_is_reported_not_retranslated(self):
        """过长段只记录 reported：不自动改写、不触发额外翻译调用。"""
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            _txt(txt)

            def handler(messages, tier, json_mode):
                system = messages[0]["content"]
                user = messages[-1]["content"]
                if "文学翻译" in system:
                    segs = _numbered_segments(user)
                    return json.dumps(
                        {"translations": ["失控重复" * 100] + [_LONG] * (len(segs) - 1)},
                        ensure_ascii=False,
                    )
                return routing_handler(messages, tier, json_mode)

            client = FakeClient(handler=handler)
            store = Orchestrator(_config(os.path.join(d, "state")), client=client).run(txt)
            chapter = store.load_chapter(0)

            self.assertEqual(chapter.text_segments[0].target, "失控重复" * 100)  # 原样保留
            self.assertEqual(len(_translation_calls(client)), 1)  # 不重译
            self.assertEqual(
                _batch_events(store)[0]["length_fixes"],
                [{"index": 0, "reason": "too_long", "action": "reported"}],
            )


if __name__ == "__main__":
    unittest.main()
