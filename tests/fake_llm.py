"""测试用：按 agent 类型路由的 FakeClient handler，驱动整条流水线（离线）。"""

from __future__ import annotations

import json
import re


def _count_numbered(text: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", text, re.M))


def routing_handler(messages, tier, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]

    if "语言识别器" in system:
        return json.dumps({"language": "ja"}, ensure_ascii=False)

    if "前期分析师" in system:
        return json.dumps({
            "genre": "校园", "tone": "冷峻", "style_guide": "克制",
            "characters": [{"source": "綾小路", "target": "绫小路", "gender": "男"}],
            "terms": [],
        }, ensure_ascii=False)

    if "标题翻译" in system:
        n = _count_numbered(user)
        return json.dumps({"titles": [f"标题{i}" for i in range(n)]}, ensure_ascii=False)

    if "文学翻译" in system:
        n = _count_numbered(user)
        return json.dumps({"translations": [f"译{i}" for i in range(n)]}, ensure_ascii=False)

    if "中文润色编辑" in system:
        n = _count_numbered(user)
        return json.dumps({"polished": [f"润{i}" for i in range(n)]}, ensure_ascii=False)

    if "译文审校" in system:
        return json.dumps({"issues": []}, ensure_ascii=False)

    if "术语抽取器" in system:
        return json.dumps({"terms": [
            {"source": "堀北", "target": "堀北", "type": "人物", "gender": "女"}
        ]}, ensure_ascii=False)

    if "回译译者" in system:
        n = _count_numbered(user)
        return json.dumps({"backtranslations": [f"逆{i}" for i in range(n)]}, ensure_ascii=False)

    if "保真度" in system:
        return json.dumps({"issues": []}, ensure_ascii=False)

    if "情节摘要员" in system:
        return "故事梗概更新。"

    return "{}" if json_mode else ""
