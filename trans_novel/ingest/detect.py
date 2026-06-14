"""来源语言自动检测（主攻 日 / 英）。

按 Unicode 脚本统计判别：
- 含平假名/片假名 → 日语（ja）；
- 否则以拉丁字母为主 → 英语（en）；
- 仅有汉字而无假名（少见的纯汉字日文）→ 退化为 ja（目标是中文，源若为汉字圈仍按 ja 提示词处理最稳）。

只区分 ja / en 两类（其余暂统一回退 en，可后续扩展）。
"""

from __future__ import annotations


def _counts(text: str) -> tuple[int, int, int]:
    """返回 (假名数, 拉丁字母数, 汉字数)。"""
    kana = latin = han = 0
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x309F or 0x30A0 <= o <= 0x30FF:   # 平/片假名
            kana += 1
        elif 0x4E00 <= o <= 0x9FFF:                          # CJK 汉字
            han += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):       # 拉丁字母
            latin += 1
    return kana, latin, han


def detect_language(text: str) -> str:
    """返回 'ja' 或 'en'。"""
    kana, latin, han = _counts(text or "")
    if kana > 0:
        return "ja"
    if latin == 0 and han > 0:
        # 纯汉字（无假名、无拉丁）：按日语提示词处理更稳妥
        return "ja"
    return "en"
