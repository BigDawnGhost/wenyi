"""模型 JSON 输出的宽松解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from json_repair import repair_json


@dataclass(frozen=True)
class JsonParseResult:
    """模型 JSON 的解析结果，以及是否经过语法修复。"""

    value: Any
    repaired: bool


def parse_json_result(text: str) -> JsonParseResult:
    """解析模型 JSON，并准确标记结果是否经过语法修复。

    这里显式执行一次 ``json.loads`` 是为了生成 ``repaired`` 标志；失败后
    调用 json-repair 时设置 ``skip_json_loads=True``，因此不会重复严格解析。
    """
    raw = (text or "").strip()
    try:
        return JsonParseResult(json.loads(raw), repaired=False)
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        value = repair_json(
            raw,
            return_objects=True,
            skip_json_loads=True,
        )
    except Exception as error:
        raise ValueError(f"无法解析为 JSON：{raw[:200]!r}") from error
    # json-repair 对空文本和纯自然语言返回空串；它们不属于可恢复 JSON。
    if value == "":
        raise ValueError(f"无法解析为 JSON：{raw[:200]!r}")
    return JsonParseResult(value, repaired=True)


def parse_json_loose(text: str) -> Any:
    """返回模型 JSON 的值；语法容错由 json-repair 统一实现。"""
    return parse_json_result(text).value
