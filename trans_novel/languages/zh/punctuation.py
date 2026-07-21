"""简体中文目标语言的确定性标点后处理。"""

from __future__ import annotations

from ...postprocess.punct import normalize_zh, normalize_zh_segments


enabled = True


def normalize(text: str) -> str:
    """把单段译文规范化为简体中文全角标点。"""
    return normalize_zh(text)


def normalize_segments(
    texts: list[str], continuations: list[bool] | None = None
) -> list[str]:
    """按逻辑原段规范化标点，并仅在切分续段间传递引号状态。"""
    return normalize_zh_segments(texts, continuations)
