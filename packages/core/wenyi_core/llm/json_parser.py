"""模型 JSON 输出的宽松解析。"""

from __future__ import annotations

import json
import re
from typing import Any


def _repair_unescaped_quotes(text: str) -> str:
    """转义 JSON 字符串值内部未转义的 ASCII 双引号。

    部分模型（尤其无原生 JSON 模式的 provider）会在译文里原样输出英文引号。
    启发式：字符串内的 `"` 后面（跳过空白）若不是 `,:]}`，视为内容引号转义之。
    中文译文以全角标点为主，误判面极小；仅作为常规解析失败后的兜底。
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
        elif c == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        elif c == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ",:]}":
                in_str = False
                out.append(c)
            else:
                out.append('\\"')
        else:
            out.append(c)
        i += 1
    return "".join(out)


def parse_json_loose(text: str) -> Any:
    """从模型输出里尽力解析 JSON。

    优先直接 json.loads；失败则剥离 ```json 围栏并截取首个 {…}/[…] 块再试。
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 去掉 markdown 代码围栏
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            text = inner
    # 截取首个 JSON 数组或对象
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    # 从首个 {/[ 起解析第一个完整 JSON 值，忽略尾部多余字符（如重复的 }）
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[min(starts) :])
            return value
        except Exception:
            pass
    # 最后兜底：修复字符串内未转义的引号，再从完整文本解析首个 JSON 值。
    # 必须先做这一步：若同时有未转义引号和尾部多余字符，直接截取内部数组
    # 会丢掉外层对象（如 {"translations": [...]}）。
    repaired = _repair_unescaped_quotes(text)
    starts = [i for i in (repaired.find("{"), repaired.find("[")) if i != -1]
    if starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(repaired[min(starts) :])
            return value
        except Exception:
            pass

    # 修复后仍无法解析时，才依次尝试完整文本和对象/数组片段。
    for candidate in (
        text,
        *(
            text[s : e + 1]
            for o, c in (("[", "]"), ("{", "}"))
            for s, e in [(text.find(o), text.rfind(c))]
            if s != -1 and e > s
        ),
    ):
        try:
            return json.loads(_repair_unescaped_quotes(candidate))
        except Exception:
            continue
    raise ValueError(f"无法解析为 JSON：{text[:200]!r}")
