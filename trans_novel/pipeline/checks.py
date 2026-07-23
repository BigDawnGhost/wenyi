"""无模型的廉价校验：句段对齐、长度比异常（疑似漏译/失控）。

这些是不花 token 的第一道关，配合审校 agent 一起用。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass


@dataclass
class LengthFlag:
    index: int
    ratio: float
    reason: str  # "too_short" | "too_long" | "empty"


def length_flags(
    sources: list[str],
    targets: list[str],
    *,
    too_short: float = 0.30,
    too_long: float = 3.0,
) -> list[LengthFlag]:
    """按 译文/原文 字符比标记可疑段。

    粗略按字符比抓异常；过小多半漏译，过大可能失控/增译。
    阈值偏宽松，只抓明显异常，避免误报。
    """
    flags: list[LengthFlag] = []
    for i, (s, t) in enumerate(zip(sources, targets)):
        s_len = len(s.strip())
        t_len = len((t or "").strip())
        if s_len == 0:
            continue
        if t_len == 0:
            flags.append(LengthFlag(i, 0.0, "empty"))
            continue
        ratio = t_len / s_len
        if ratio < too_short:
            flags.append(LengthFlag(i, ratio, "too_short"))
        elif ratio > too_long:
            flags.append(LengthFlag(i, ratio, "too_long"))
    return flags


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _comparable_text(text: str) -> str:
    """规整不影响语义的空白和兼容字符，用于识别实际未改写的候选。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    return "".join(normalized.split())


def _number_tokens(text: str) -> Counter[str]:
    """提取可安全做确定性比对的阿拉伯数字记号及其出现次数。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    return Counter(_NUMBER_RE.findall(normalized))


def repair_rejection_reason(
    source: str,
    current_target: str,
    proposed_target: str,
) -> str | None:
    """返回自动修复候选的确定性拒绝原因；通过则返回 ``None``。

    这里只检查不需要模型判断的硬条件。语义是否更忠实、是否真的修正审校
    意见等开放问题，留给独立修复验证员。数字规则仅要求保留原文和旧译文
    都明确包含的数字，避免把旧译文已经稳定传达的信息意外删掉。
    """
    current = _comparable_text(current_target)
    proposed = _comparable_text(proposed_target)
    if not proposed:
        return "empty"
    if proposed == current:
        return "unchanged"

    flags = length_flags([source], [proposed_target])
    if flags:
        return flags[0].reason

    required_numbers = _number_tokens(source) & _number_tokens(current_target)
    missing_numbers = required_numbers - _number_tokens(proposed_target)
    if missing_numbers:
        return "dropped_number"
    return None
