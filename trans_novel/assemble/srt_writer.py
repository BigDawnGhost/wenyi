"""SRT 字幕写出。"""

from __future__ import annotations

import os

from ..ingest.srt_reader import SrtCue
from .writer_common import _ensure_parent_dir, bilingual_out_path


def default_srt_out_paths(
    source_path: str,
    *,
    out: str | None = None,
    mono: bool = True,
    bilingual: bool = False,
) -> tuple[str | None, str | None]:
    """返回 (单语 .srt, 双语 .srt)；未开启的一侧为 None。"""
    mono_path: str | None = None
    bilingual_path: str | None = None
    if mono:
        if out is not None:
            mono_path = out if out.lower().endswith(".srt") else f"{out}.srt"
        else:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), "output")
            stem = os.path.splitext(os.path.basename(source_path))[0]
            mono_path = os.path.join(output_dir, f"{stem}.zh.srt")
        _ensure_parent_dir(mono_path)
    if bilingual:
        if out is not None:
            base = out if out.lower().endswith(".srt") else f"{out}.srt"
            bilingual_path = bilingual_out_path(base)
        else:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), "output")
            stem = os.path.splitext(os.path.basename(source_path))[0]
            bilingual_path = os.path.join(output_dir, f"{stem}.zh-bi.srt")
        _ensure_parent_dir(bilingual_path)
    return mono_path, bilingual_path


def write_srt_outputs(
    cues: list[SrtCue],
    translations: dict[str, str],
    *,
    mono_path: str | None,
    bilingual_path: str | None,
) -> list[str]:
    """写出单语 / 双语 SRT；返回实际写入路径列表。"""
    written: list[str] = []
    if mono_path:
        blocks = [
            f"{cue.index}\n{cue.timestamp}\n{translations.get(cue.index, cue.text)}\n"
            for cue in cues
        ]
        with open(mono_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(blocks))
        written.append(mono_path)
    if bilingual_path:
        blocks = [
            f"{cue.index}\n{cue.timestamp}\n{translations.get(cue.index, cue.text)}\n{cue.text}\n"
            for cue in cues
        ]
        with open(bilingual_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(blocks))
        written.append(bilingual_path)
    return written
