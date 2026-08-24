"""SRT 字幕解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SRT_BLOCK = re.compile(
    r"(\d+)\r?\n(\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3})\r?\n"
    r"([\s\S]*?)(?=\r?\n\r?\n|\Z)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SrtCue:
    """一条字幕：序号、时间轴、正文。"""

    index: str
    timestamp: str
    text: str


def _decode_srt_bytes(raw: bytes) -> str:
    """按常见编码尝试解码；全部失败时用替换策略保证可跑。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_srt(path: str) -> list[SrtCue]:
    """读取并解析 SRT；文件不可读或无有效块时抛 ValueError。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        raise ValueError(f"无法读取字幕文件：{error}") from error

    content = _decode_srt_bytes(raw)
    matches = _SRT_BLOCK.findall(content)
    if not matches:
        raise ValueError("未解析到有效 SRT 字幕块")
    return [SrtCue(index=m[0], timestamp=m[1], text=m[2].strip()) for m in matches]
