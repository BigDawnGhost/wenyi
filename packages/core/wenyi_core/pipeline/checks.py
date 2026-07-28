"""无模型的廉价校验：句段对齐、长度比异常（疑似漏译/失控）。

这些是不花 token 的第一道关，配合审校 agent 一起用。
"""

from __future__ import annotations

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
