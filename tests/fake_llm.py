"""Offline FakeClient handler routing agent tasks through the complete pipeline."""

from __future__ import annotations

import json
import re

from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.usage import UsageSample

METERED_TOTAL_TOKENS = 8  # prompt 5 + completion 3, recorded by MeteredFakeClient per call.


def _count_numbered(text: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", text, re.MULTILINE))


def routing_handler(messages, tier, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]

    if "language detector" in system:
        return json.dumps({"language": "ja"}, ensure_ascii=False)

    if "pre-translation analyst" in system:
        return json.dumps(
            {
                "genre": "校园",
                "tone": "冷峻",
                "style_guide": "克制",
                "characters": [{"source": "綾小路", "target": "绫小路", "gender": "male"}],
                "terms": [],
            },
            ensure_ascii=False,
        )

    if "chapter title translator" in system:
        n = _count_numbered(user)
        return json.dumps({"titles": [f"标题{i}" for i in range(n)]}, ensure_ascii=False)

    if "literary translator" in system:
        n = _count_numbered(user)
        return json.dumps({"translations": [f"译{i}" for i in range(n)]}, ensure_ascii=False)

    if "Simplified Chinese prose editor" in system:
        n = _count_numbered(user)
        return json.dumps({"polished": [f"润{i}" for i in range(n)]}, ensure_ascii=False)

    if "translation reviewer" in system:
        n = _count_numbered(user)
        return json.dumps(
            {
                "issues": [],
                "reviewed_segments": n,
                "complete": True,
            },
            ensure_ascii=False,
        )

    if "terminology" in system and "extractor" in system:
        return json.dumps(
            {"terms": [{"source": "堀北", "target": "堀北", "type": "person", "gender": "female"}]},
            ensure_ascii=False,
        )

    if "chapter digest writer" in system:
        return "本章梗概：人物登场，情节推进。"

    if "whole-book synopsis writer" in system:
        return "全书概览：主线与人物关系，整体基调。"

    return "{}" if json_mode else ""


class MeteredFakeClient(FakeClient):
    """Record fixed small usage per offline call so tests can assert stage-level accounting."""

    def complete(
        self,
        messages,
        *,
        operation,
        json_mode=False,
        max_tokens=None,
    ):
        self.usage.record(
            self.routes[operation].tier or "direct",
            UsageSample(
                prompt_tokens=5,
                completion_tokens=3,
                total_tokens=8,
                cache_miss_tokens=5,
            ),
            operation,
        )
        return super().complete(
            messages,
            operation=operation,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
