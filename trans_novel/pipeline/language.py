"""流水线共享的语言规范化工具。

该模块只包含与具体流水线阶段无关的纯函数，供 Runtime 和准备服务共同
使用，避免基础运行时反向依赖某个领域服务。
"""

from __future__ import annotations

# 语言名/代码 → ISO 639-1 两字母代码（模型检测结果归一化）
_LANG_ALIASES = {
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


def normalize_lang(code: str) -> str:
    """把模型返回的语言名或别名规整为 ISO 639-1 两字母代码。"""
    normalized = (code or "").strip().lower()
    if not normalized or normalized in {
        "auto",
        "unknown",
        "und",
        "uncertain",
        "mixed",
        "多语言",
        "未知",
    }:
        return ""
    if normalized in _LANG_ALIASES:
        return _LANG_ALIASES[normalized]
    return normalized[:2] if normalized[:2].isalpha() else ""
