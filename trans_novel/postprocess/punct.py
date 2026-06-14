"""译文标点规范化 —— 统一为简体中文大陆通用全角标点。

确定性兜底（提示词已要求，这里再保一道）：
- 日式引号 「」→ “”，『』→ ‘’；
- 英式直引号 "→ “/”（按出现次序配对），' → ‘/’（按次序配对，撇号尽量保留）；
- 半角 , . ! ? : ; 在中文语境（相邻为 CJK）→ 全角 ，。！？：；；
- 连续点号 ... / 。。。 / ・・・ → ……；-- 或 — → ——。

策略保守：英文/数字串内部的半角标点（如 9.11、Mr. Smith）不误伤——
仅当半角标点紧邻 CJK 字符时才转全角。
"""

from __future__ import annotations

import re

_CJK = (
    "一-鿿"      # CJK 统一汉字
    "぀-ヿ"      # 假名（保险）
    "＀-￯"      # 全角符号
    "“”‘’（）《》【】、，。！？：；…—"
)
_CJK_RE = f"[{_CJK}]"

# 半角标点 → 全角
_HALF_TO_FULL = {",": "，", ".": "。", "!": "！", "?": "？", ":": "：", ";": "；"}


def _convert_quotes(text: str) -> str:
    # 日式引号直接映射
    text = text.translate(str.maketrans({"「": "“", "」": "”", "『": "‘", "』": "’"}))

    # 英式直双引号：按出现次序交替配对 → “ ”
    out = []
    open_dq = True
    for ch in text:
        if ch == '"':
            out.append("“" if open_dq else "”")
            open_dq = not open_dq
        else:
            out.append(ch)
    text = "".join(out)

    # 直单引号：仅当成对出现于引用语境时转弯引号；撇号（被字母包夹）保留为 ’
    def _single(m: re.Match) -> str:
        return "’"  # 英文撇号统一为右单引号字形
    text = re.sub(r"(?<=[A-Za-z])'(?=[A-Za-z])", _single, text)
    # 其余成对单引号交替配对
    out, open_sq = [], True
    for ch in text:
        if ch == "'":
            out.append("‘" if open_sq else "’")
            open_sq = not open_sq
        else:
            out.append(ch)
    return "".join(out)


def _convert_ellipsis_dash(text: str) -> str:
    text = re.sub(r"。{3,}", "……", text)
    text = re.sub(r"・{2,}", "……", text)
    text = re.sub(r"\.{3,}", "……", text)
    text = re.sub(r"…+", "……", text)          # 单个/多个 … → ……
    text = re.sub(r"-{2,}", "——", text)
    text = re.sub(r"—{1,}", "——", text)        # — / —— 归一为 ——
    text = re.sub(r"——(——)+", "——", text)
    return text


def _convert_halfwidth(text: str) -> str:
    """半角 ,.!?:; 紧邻 CJK 时转全角。"""
    def repl(m: re.Match) -> str:
        return _HALF_TO_FULL[m.group(0)]

    # 标点左侧或右侧是 CJK 即转（避免误伤英文/数字内部）
    pattern = re.compile(
        rf"(?<={_CJK_RE})[,.!?:;]|[,.!?:;](?={_CJK_RE})"
    )
    return pattern.sub(repl, text)


def normalize_zh(text: str) -> str:
    """把一段中文译文的标点规范化为简体中文通用全角标点。"""
    if not text:
        return text
    text = _convert_quotes(text)
    text = _convert_ellipsis_dash(text)
    text = _convert_halfwidth(text)
    # 全角标点后的多余空格清理（中文标点后不留空格）
    text = re.sub(r"([，。！？：；、”’》】])\s+", r"\1", text)
    return text
