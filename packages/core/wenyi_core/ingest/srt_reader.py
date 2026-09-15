"""Parse SRT subtitles."""

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
    """A subtitle cue with index, timing and body text."""

    index: str
    timestamp: str
    text: str


def _decode_srt_bytes(raw: bytes) -> str:
    """Try common encodings, then use replacement decoding if all fail."""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_srt(path: str) -> list[SrtCue]:
    """Read SRT cues; raise ValueError when the file is unreadable or has no valid blocks."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as error:
        raise ValueError(f"Cannot read subtitle file: {error}") from error

    content = _decode_srt_bytes(raw)
    matches = _SRT_BLOCK.findall(content)
    if not matches:
        raise ValueError("No valid SRT subtitle blocks found")
    return [SrtCue(index=m[0], timestamp=m[1], text=m[2].strip()) for m in matches]
