"""Language-tag parsing and output metadata helpers."""

from __future__ import annotations

import re


SIMPLIFIED_CHINESE_TAGS = {
    "zh",
    "zh-cn",
    "zh-sg",
    "zh-hans",
    "zh-hans-cn",
    "zh-hans-sg",
}

LANGUAGE_ALIASES = {
    "jp": "ja",
    "jpn": "ja",
    "japanese": "ja",
    "日语": "ja",
    "日文": "ja",
    "ja-jp": "ja",
    "eng": "en",
    "english": "en",
    "英语": "en",
    "英文": "en",
    "en-us": "en",
    "en-gb": "en",
    "rus": "ru",
    "russian": "ru",
    "俄语": "ru",
    "俄文": "ru",
    "kor": "ko",
    "korean": "ko",
    "韩语": "ko",
    "韩文": "ko",
    "zho": "zh",
    "chi": "zh",
    "chinese": "zh",
    "中文": "zh",
    "汉语": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "法语": "fr",
    "法文": "fr",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "德语": "de",
    "德文": "de",
    "spa": "es",
    "spanish": "es",
    "西班牙语": "es",
    "西班牙文": "es",
    "ita": "it",
    "italian": "it",
    "意大利语": "it",
    "意大利文": "it",
    "por": "pt",
    "portuguese": "pt",
    "葡萄牙语": "pt",
    "葡萄牙文": "pt",
}


def canonical_tag(tag: str | None) -> str:
    """Return a conservatively normalized BCP 47 language tag."""
    raw = (tag or "").strip().replace("_", "-")
    if not raw:
        return ""
    parts = raw.split("-")
    normalized = [parts[0].lower()]
    in_extension = False
    for part in parts[1:]:
        if len(part) == 1 and part.isalnum():
            normalized.append(part.lower())
            in_extension = True
        elif in_extension:
            normalized.append(part.lower())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        elif len(part) == 3 and part.isdigit():
            normalized.append(part)
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def base_language(tag: str | None) -> str:
    """Return the lowercase primary language subtag."""
    return canonical_tag(tag).split("-", 1)[0]


def normalize_detected_language(value: str | None) -> str:
    """Normalize a model-returned language name or code to ISO 639-1."""
    raw = (value or "").strip().lower().replace("_", "-")
    if raw in {
        "",
        "auto",
        "unknown",
        "und",
        "uncertain",
        "mixed",
        "多语言",
        "未知",
    }:
        return ""
    raw = raw.split(",", 1)[0].split(" ", 1)[0]
    if raw in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[raw]
    base = raw.split("-", 1)[0]
    return base if len(base) == 2 and base.isalpha() else ""


def target_identity(tag: str | None) -> str:
    """Return the identity used to compare target-language checkpoints."""
    canonical = canonical_tag(tag)
    if canonical.lower() in SIMPLIFIED_CHINESE_TAGS:
        return "zh"
    return canonical


def is_simplified_chinese(tag: str | None) -> bool:
    """Return whether a target uses Wenyi's Simplified Chinese bundle."""
    return target_identity(tag) == "zh"


def epub_language_tag(tag: str | None) -> str:
    """Return a target tag suitable for EPUB metadata."""
    canonical = canonical_tag(tag)
    if not canonical or is_simplified_chinese(canonical):
        return "zh-Hans"
    return canonical


def filename_language_tag(tag: str | None) -> str:
    """Return a filesystem-safe target-language suffix."""
    canonical = canonical_tag(tag) or "translated"
    return re.sub(r"[^A-Za-z0-9-]+", "-", canonical).strip("-") or "translated"
