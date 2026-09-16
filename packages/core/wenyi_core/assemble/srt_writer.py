"""Write SRT subtitles."""

from __future__ import annotations

import os

from ..i18n.languages import require_language
from ..ingest.srt_reader import SrtCue
from .writer_common import _ensure_parent_dir, bilingual_out_path


def default_srt_out_paths(
    source_path: str,
    *,
    out: str | None = None,
    mono: bool = True,
    bilingual: bool = False,
    target_lang: str = "zh",
) -> tuple[str | None, str | None]:
    """Return monolingual and bilingual SRT paths; use None for disabled outputs."""
    mono_path: str | None = None
    bilingual_path: str | None = None
    language = require_language(target_lang)
    if mono:
        if out is not None:
            mono_path = out if out.lower().endswith(".srt") else f"{out}.srt"
        else:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), "output")
            stem = os.path.splitext(os.path.basename(source_path))[0]
            mono_path = os.path.join(output_dir, f"{stem}.{language}.srt")
        _ensure_parent_dir(mono_path)
    if bilingual:
        if out is not None:
            base = out if out.lower().endswith(".srt") else f"{out}.srt"
            bilingual_path = bilingual_out_path(base)
        else:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(source_path)), "output")
            stem = os.path.splitext(os.path.basename(source_path))[0]
            bilingual_path = os.path.join(output_dir, f"{stem}.{language}-bi.srt")
        _ensure_parent_dir(bilingual_path)
    return mono_path, bilingual_path


def write_srt_outputs(
    cues: list[SrtCue],
    translations: dict[str, str],
    *,
    mono_path: str | None,
    bilingual_path: str | None,
) -> list[str]:
    """Write enabled monolingual/bilingual SRT files and return their paths."""
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
