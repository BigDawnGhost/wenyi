"""管线层与呈现层共享的语言代码规范化。"""

from __future__ import annotations


def normalize_lang_code(code: str | None) -> str:
    aliases = {
        "japanese": "ja",
        "日语": "ja",
        "日文": "ja",
        "jp": "ja",
        "jpn": "ja",
        "english": "en",
        "英语": "en",
        "英文": "en",
        "eng": "en",
        "russian": "ru",
        "俄语": "ru",
        "俄文": "ru",
        "rus": "ru",
        "chinese": "zh",
        "中文": "zh",
        "汉语": "zh",
        "zh-cn": "zh",
        "zho": "zh",
        "korean": "ko",
        "韩语": "ko",
        "韩文": "ko",
        "kor": "ko",
        "french": "fr",
        "法语": "fr",
        "法文": "fr",
        "german": "de",
        "德语": "de",
        "德文": "de",
        "spanish": "es",
        "西班牙语": "es",
        "西班牙文": "es",
        "italian": "it",
        "意大利语": "it",
        "意大利文": "it",
        "portuguese": "pt",
        "葡萄牙语": "pt",
        "葡萄牙文": "pt",
    }
    c = (code or "").strip().lower()
    if not c or c in {"auto", "unknown", "und", "uncertain", "mixed", "多语言", "未知"}:
        return ""
    return aliases.get(c, c[:2] if c[:2].isalpha() else "")


__all__ = ["normalize_lang_code"]
