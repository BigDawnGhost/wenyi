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


def _convert_quotes(
    text: str,
    *,
    double_open: bool = True,
    single_open: bool = True,
) -> tuple[str, bool, bool]:
    """转换日式和 ASCII 引号，并返回处理后的单双引号开闭状态。"""
    # 日式引号直接映射
    text = text.translate(str.maketrans({"「": "“", "」": "”", "『": "‘", "』": "’"}))

    # 英式直双引号：按出现次序交替配对 → “ ”
    out = []
    for ch in text:
        if ch == '"':
            out.append("“" if double_open else "”")
            double_open = not double_open
        else:
            out.append(ch)
    text = "".join(out)

    # 直单引号：字母内撇号不改变引号状态；词尾撇号与右引号都输出 ’，
    # 但只有当前位于引语内时才关闭引号。
    out = []
    for index, ch in enumerate(text):
        if ch == "'":
            before = text[index - 1] if index else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            before_letter = before.isascii() and before.isalpha()
            after_letter = after.isascii() and after.isalpha()
            if before_letter and after_letter:
                out.append("’")
            elif before_letter and not single_open:
                out.append("’")
                single_open = True
            elif before_letter:
                out.append("’")
            else:
                out.append("‘" if single_open else "’")
                single_open = not single_open
        else:
            out.append(ch)
    return "".join(out), double_open, single_open


def _convert_ellipsis_dash(text: str) -> str:
    """把多种省略号和破折号写法统一为中文双字符形式。"""
    text = re.sub(r"。{3,}", "……", text)
    text = re.sub(r"・{2,}", "……", text)
    text = re.sub(r"\.{3,}", "……", text)
    text = re.sub(r"…+", "……", text)          # 单个/多个 … → ……
    text = re.sub(r"-{2,}", "——", text)
    text = re.sub(r"—{1,}", "——", text)        # — / —— 归一为 ——
    return text


def _convert_halfwidth(text: str) -> str:
    """半角 ,.!?:; 紧邻 CJK 时转全角。"""
    def repl(m: re.Match) -> str:
        """按映射表替换一个已匹配的半角标点。"""
        return _HALF_TO_FULL[m.group(0)]

    # 标点左侧是 CJK 时转换；只与右侧 CJK 相邻时，若左侧是 ASCII
    # 字母/数字则保留，避免把 Mr.王、v2.版本 之类的边界误改。
    pattern = re.compile(
        rf"(?<={_CJK_RE})[,.!?:;]|[,.!?:;](?={_CJK_RE})"
    )
    return pattern.sub(
        lambda match: (
            match.group(0)
            if match.start() > 0 and text[match.start() - 1].isascii()
            and text[match.start() - 1].isalnum()
            else repl(match)
        ),
        text,
    )


def _normalize_with_quote_state(
    text: str,
    *,
    double_open: bool,
    single_open: bool,
) -> tuple[str, bool, bool]:
    """在给定引号状态下完成一段规范化，并返回新的状态。"""
    if not text:
        return text, double_open, single_open
    text, double_open, single_open = _convert_quotes(
        text,
        double_open=double_open,
        single_open=single_open,
    )
    text = _convert_ellipsis_dash(text)
    text = _convert_halfwidth(text)
    text = re.sub(r"([，。！？：；、])\s+", r"\1", text)
    text = re.sub(rf"([”’》】])\s+(?={_CJK_RE})", r"\1", text)
    return text, double_open, single_open


def normalize_zh(text: str) -> str:
    """把一段中文译文的标点规范化为简体中文通用全角标点。"""
    normalized, _, _ = _normalize_with_quote_state(
        text,
        double_open=True,
        single_open=True,
    )
    return normalized


def normalize_zh_segments(
    texts: list[str],
    continuations: list[bool] | None = None,
) -> list[str]:
    """按逻辑原段规范化标点，只在 cont=True 的切分续段间传递状态。

    普通段落即使缺失引号也不会改变下一段的开闭判断，避免错误级联污染后文。
    """
    if continuations is None:
        continuations = [False] * len(texts)
    if len(continuations) != len(texts):
        raise ValueError("texts 与 continuations 数量必须一致")

    normalized: list[str] = []
    double_open = True
    single_open = True
    for index, (text, continuation) in enumerate(zip(texts, continuations)):
        if index == 0 or not continuation:
            double_open = True
            single_open = True
        value, double_open, single_open = _normalize_with_quote_state(
            text,
            double_open=double_open,
            single_open=single_open,
        )
        normalized.append(value)
    return normalized
