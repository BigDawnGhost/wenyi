"""Public metadata helpers for assembled EPUB output."""

from __future__ import annotations

from trans_novel.postprocess.punct import normalize_heading_numbering


def epub_language(lang: str | None) -> str:
    """EPUB 元数据语言码；中文目标默认标成简体中文。"""
    normalized = (lang or "").strip().replace("_", "-").lower()
    if normalized in {"", "zh", "zh-cn", "zh-hans", "cn"}:
        return "zh-Hans"
    return lang or "zh-Hans"


def translated_toc_title(entry: dict[str, object]) -> str:
    """Return the effective TOC title, preserving declared source labels."""
    value = (
        entry.get("title")
        if entry.get("preserve_source") is True
        else entry.get("title_translated") or entry.get("title")
    )
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    return (
        stripped if entry.get("preserve_source") is True else normalize_heading_numbering(stripped)
    )


__all__ = ["epub_language", "translated_toc_title"]
