"""测试用：按 agent 类型路由的 FakeClient handler，驱动整条流水线（离线）。"""

from __future__ import annotations

import json
import math
import re


def _count_numbered(text: str) -> int:
    return len(re.findall(r"^\[(\d+)\]", text, re.MULTILINE))


def _numbered_segments(text: str) -> list[tuple[int, str]]:
    """从 user prompt 抽取 [i] 编号段及其文本，供按源段长度确定性填充译文。

    段捕获在下一条编号、空行（user 模板末尾的指令行前）或文本结尾处停止，
    因此不会吞掉"请翻译以上每一段…"之类的指令行；段内单换行可完整捕获。
    若未来 user 模板在编号块后不再以空行接指令行，需回看本 regex。
    """
    return [
        (int(i), s)
        for i, s in re.findall(r"^\[(\d+)\] (.*?)(?=^\[\d+\] |\n\n|\Z)", text, re.M | re.S)
    ]


def routing_handler(messages, tier, json_mode):
    system = messages[0]["content"]
    user = messages[-1]["content"]

    if "语言识别器" in system:
        return json.dumps({"language": "ja"}, ensure_ascii=False)

    if "前期分析师" in system:
        english = "给译者的英文写作风格指南" in system
        return json.dumps(
            {
                "genre": "校园",
                "tone": "冷峻",
                "style_guide": "克制",
                "characters": [
                    {
                        "source": "綾小路" if not english else "林远",
                        "target": "绫小路" if not english else "Lin Yuan",
                        "gender": "男",
                    }
                ],
                "terms": [],
            },
            ensure_ascii=False,
        )

    if "标题翻译" in system:
        n = _count_numbered(user)
        prefix = "Title" if "翻译为英文" in system else "标题"
        return json.dumps({"titles": [f"{prefix}{i}" for i in range(n)]}, ensure_ascii=False)

    if "文学翻译" in system:
        prefix = "Translation" if "翻译为英文" in system else "译"
        segs = _numbered_segments(user)
        if not segs:
            # 兜底：编号块解析异常时维持原计数行为
            segs = [(i, "") for i in range(_count_numbered(user))]
        out = []
        for i, seg in segs:
            base = f"{prefix}{i}"
            # 按源段长度填充到 0.6 倍，保证首译长度门在 fake 数据下从不触发
            # （两个方向 too_short 阈值 0.3 / 0.5 都安全，且不触 too_long）
            out.append(base + "x" * max(0, math.ceil(0.6 * len(seg)) - len(base)))
        return json.dumps({"translations": out}, ensure_ascii=False)

    if "文学润色编辑" in system:
        prefix = "Polished" if "英文文学润色编辑" in system else "润"
        segs = _numbered_segments(user)
        if not segs:
            segs = [(i, "") for i in range(_count_numbered(user))]
        out = []
        for i, seg in segs:
            base = f"{prefix}{i}"
            # 输出与输入等长（≈0.6× 源长）：通过门的润色后检查，防止回退误触发
            out.append(base + "x" * max(0, len(seg) - len(base)))
        return json.dumps({"polished": out}, ensure_ascii=False)

    if "译文审校" in system:
        n = _count_numbered(user)
        return json.dumps(
            {
                "issues": [],
                "reviewed_segments": n,
                "complete": True,
            },
            ensure_ascii=False,
        )

    if "术语" in system and "抽取器" in system:
        if "英文译文" in system:
            return json.dumps(
                {"terms": [{"source": "林远", "target": "Lin Yuan", "type": "人物"}]},
                ensure_ascii=False,
            )
        return json.dumps(
            {"terms": [{"source": "堀北", "target": "堀北", "type": "人物", "gender": "女"}]},
            ensure_ascii=False,
        )

    if "回译译者" in system:
        n = _count_numbered(user)
        return json.dumps({"backtranslations": [f"逆{i}" for i in range(n)]}, ensure_ascii=False)

    if "保真度" in system:
        return json.dumps({"issues": []}, ensure_ascii=False)

    if "章节梗概员" in system:
        return "本章梗概：人物登场，情节推进。"

    if "全书概览员" in system:
        return "全书概览：主线与人物关系，整体基调。"

    return "{}" if json_mode else ""
