"""测试用：按 agent 类型路由的 FakeClient handler，驱动整条流水线（离线）。"""

from __future__ import annotations

import json
import re


def _count_numbered(text: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", text, re.M))


def routing_handler(messages, tier, json_mode):
    """Return deterministic zh/en fixtures while routing by the agent role."""
    system = messages[0]["content"]
    user = messages[-1]["content"]
    english_target = "English" in system

    if "语言识别器" in system or "Identify the main natural language" in system:
        return json.dumps({"language": "ja"}, ensure_ascii=False)

    if "前期分析师" in system or "pre-translation analyst" in system:
        if english_target:
            data = {
                "genre": "school fiction",
                "tone": "restrained",
                "style_guide": "Use restrained literary English.",
                "characters": [
                    {
                        "source": "綾小路",
                        "target": "Ayanokoji",
                        "type": "person",
                        "gender": "male",
                        "note": "A reserved male student.",
                    }
                ],
                "terms": [],
            }
        else:
            data = {
                "genre": "校园", "tone": "冷峻", "style_guide": "克制",
                "characters": [
                    {
                        "source": "綾小路",
                        "target": "绫小路",
                        "type": "人物",
                        "gender": "男",
                        "note": "性格克制的男学生。",
                    }
                ],
                "terms": [],
            }
        return json.dumps(data, ensure_ascii=False)

    if "标题翻译" in system or "translate novel chapter titles" in system:
        n = _count_numbered(user)
        prefix = "Chapter " if english_target else "标题"
        return json.dumps(
            {"titles": [f"{prefix}{i}" for i in range(n)]}, ensure_ascii=False
        )

    if "文学翻译" in system or "senior literary translator" in system:
        n = _count_numbered(user)
        translations = (
            [f'He said, “Hello {i}?”' for i in range(n)]
            if english_target
            else [f"译{i}" for i in range(n)]
        )
        return json.dumps({"translations": translations}, ensure_ascii=False)

    if "文学润色编辑" in system or "literary editor working" in system:
        n = _count_numbered(user)
        polished = (
            [f'He said, “Hello {i}?”' for i in range(n)]
            if english_target
            else [f"润{i}" for i in range(n)]
        )
        return json.dumps({"polished": polished}, ensure_ascii=False)

    if "译文审校" in system or "strict translation reviewer" in system:
        return json.dumps({"issues": []}, ensure_ascii=False)

    if ("术语" in system and "抽取器" in system) or (
        "extract terminology" in system
    ):
        target = "Horikita" if english_target else "堀北"
        note = "A female student." if english_target else "女学生。"
        term_type = "person" if english_target else "人物"
        gender = "female" if english_target else "女"
        return json.dumps({"terms": [
            {
                "source": "堀北",
                "target": target,
                "type": term_type,
                "gender": gender,
                "note": note,
            }
        ]}, ensure_ascii=False)

    if "回译译者" in system or "backtranslator" in system:
        n = _count_numbered(user)
        return json.dumps({"backtranslations": [f"逆{i}" for i in range(n)]}, ensure_ascii=False)

    if "保真度" in system or "translation fidelity" in system:
        return json.dumps({"issues": []}, ensure_ascii=False)

    if "章节梗概员" in system or "summarize novel chapters" in system:
        return (
            "A character appears and the plot advances."
            if english_target
            else "本章梗概：人物登场，情节推进。"
        )

    if "全书概览员" in system or "whole-book synopsis" in system:
        return (
            "The main plot, character relationships, and overall tone."
            if english_target
            else "全书概览：主线与人物关系，整体基调。"
        )

    return "{}" if json_mode else ""
