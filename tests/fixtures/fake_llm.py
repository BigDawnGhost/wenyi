"""测试用：按 agent 类型路由的 FakeClient handler，驱动整条流水线（离线）。"""

from __future__ import annotations

import json
import re


def fake_llm_dict(*, models=("p",), extra_agents=None) -> dict:
    """构造离线 fake provider 的四角色精简配置。"""
    if extra_agents is not None:
        raise ValueError("新配置不支持 Agent 路由覆盖")
    if not models or len(models) > 4:
        raise ValueError("fake 模型配置必须包含 1 至 4 个模型")
    qualified = [f"fake/{model}" for model in models]
    translator = qualified[:1]
    analyst = qualified[:1] if len(models) < 3 else qualified[1:2]
    editor = analyst if len(models) < 4 else qualified[2:3]
    fast = qualified[:1] if len(models) == 1 else qualified[-1:]
    return {
        "models": {
            "translator": translator,
            "analyst": analyst,
            "editor": editor,
            "fast": fast,
        },
    }


def _count_numbered(text: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", text, re.M))


def _numbered_values(text: str) -> list[str]:
    return re.findall(r"^\[\d+\]\s*(.*)$", text, re.M)


def routing_handler(messages, agent, operation, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]
    if operation == "chapter.classify":
        request = json.loads(user)
        return json.dumps(
            {
                "chapter_id": request["chapter_id"],
                "kind": "translatable",
                "reason": "contains substantive source text",
            }
        )
    if operation == "layout.classify":
        request = json.loads(user)
        return json.dumps(
            {
                "observations": [
                    {"node_id": sample["node_id"], "role": "body", "level": None}
                    for sample in request["samples"]
                ]
            }
        )

    if "语言识别器" in system:
        return json.dumps({"language": "ja"}, ensure_ascii=False)

    if "前期分析师" in system:
        return json.dumps(
            {
                "genre": "校园",
                "tone": "冷峻",
                "style_guide": "克制",
                "characters": [{"source": "綾小路", "target": "绫小路", "gender": "男"}],
                "terms": [],
                "conventions": "年代统一用'20世纪90年代'；星期统一用'星期X'。",
            },
            ensure_ascii=False,
        )

    if "标题翻译" in system:
        n = _count_numbered(user)
        return json.dumps({"titles": [f"标题{i}" for i in range(n)]}, ensure_ascii=False)

    if operation == "translate.repair":
        current = (
            user.split("【当前译文】", 1)[-1].split("\n\n【唯一需要修复的问题】", 1)[0].strip()
        )
        issue_type = user.split("类型：", 1)[-1].split("\n", 1)[0].strip()
        if issue_type == "quote_loss" and current:
            return f"“{current.strip('“”「」『』《》')}”"
        return current

    if "文学翻译" in system:
        if not json_mode:
            source = user.rsplit("】", 1)[-1].strip()
            return "译" + "文" * max(1, len(source) - 1)
        sources = _numbered_values(user.split("【待译", 1)[-1])
        translations = [
            f"译{i}" + "文" * max(0, len(source) - 2) for i, source in enumerate(sources)
        ]
        return json.dumps({"translations": translations}, ensure_ascii=False)

    if operation == "polish.batch":
        suffix = user.split("【待润色段落（JSON）】", 1)[-1].lstrip()
        items, _ = json.JSONDecoder().raw_decode(suffix)
        polished = [
            {
                "id": item["id"],
                "text": f"润{i}" + "文" * max(0, len(item["target"]) - 2),
            }
            for i, item in enumerate(items)
        ]
        return json.dumps({"polished": polished}, ensure_ascii=False)

    if "术语候选挖掘" in system:
        return json.dumps({"candidates": ["堀北"]}, ensure_ascii=False)

    if "全书定名" in system:
        surfaces = re.findall(r"^\[\d+\] (\S+?)（", user, re.M)
        return json.dumps(
            {
                "terms": [
                    {"source": s, "target": s, "type": "人物", "gender": "女"}
                    for s in dict.fromkeys(surfaces)
                ]
            },
            ensure_ascii=False,
        )

    if "术语" in system and "抽取器" in system:
        return json.dumps(
            {"terms": [{"source": "堀北", "target": "堀北", "type": "人物", "gender": "女"}]},
            ensure_ascii=False,
        )

    return "{}" if json_mode else ""
