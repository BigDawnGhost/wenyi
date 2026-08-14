"""旧版 Orchestrator 使用的窄源语言检测服务。

本模块只保留旧运行路径需要的模型调用和宽松候选归一化；它不负责新版
Workflow 身份、持久化准入或 source/target 决策，也不依赖 Config、RunStore
或图运行时。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

_DETECTION_SAMPLE_CHARS = 1500
_UNRESOLVED_CANDIDATES = frozenset(
    {"auto", "unknown", "und", "uncertain", "mixed", "多语言", "未知"}
)

# 模型历史上会返回语言名称、中文名称或三字母代码；这里保持旧入口的兼容面。
_LANGUAGE_CANDIDATE_ALIASES = {
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

_DETECTION_SYSTEM_PROMPT = (
    "你是语言识别器。判断给定文本的主要自然语言，"
    '仅输出 JSON：{"language":"<ISO 639-1 两字母代码，如 ja/en/ru/ko/fr/de/zh>"}。'
    "无法判断时 language 置为空字符串。"
)


class LanguageJsonCompletion(Protocol):
    """语言检测器需要的最小 JSON 模型调用接口。"""

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> object:
        """返回已经解析的 JSON 值。"""
        ...


class SourceLanguageDetector(Protocol):
    """把纯源文样本解析成非空原始语言候选的运行时端口。"""

    def detect(self, sample: str) -> str:
        """返回待准入层验证的原始候选；失败时抛出检测错误。"""
        ...


class LanguageDetectionError(RuntimeError):
    """可由准入层安全分类的语言检测失败基类。"""

    code = "language_detection_failed"
    retryable = False


class EmptyLanguageSample(LanguageDetectionError):
    """输入中没有可供模型判断的正文。"""

    code = "language_detection_empty_sample"


class LanguageDetectionUnavailable(LanguageDetectionError):
    """模型调用暂时不可用；原始异常只保留在 cause 链中。"""

    code = "language_detection_unavailable"
    retryable = True


class InvalidLanguageDetection(LanguageDetectionError):
    """模型成功响应，但没有给出可持久化的明确语言代码。"""

    code = "language_detection_invalid_output"
    retryable = True


@dataclass(frozen=True, slots=True)
class ModelSourceLanguageDetector:
    """通过窄 ``complete_json`` 端口提取一次非空模型语言候选。"""

    client: LanguageJsonCompletion

    def detect(self, sample: str) -> str:
        """使用旧 prompt、cheap tier 和 stage 名返回未截断的原始候选。"""
        request_sample = _detection_sample(sample)
        try:
            data = self.client.complete_json(
                [
                    {"role": "system", "content": _DETECTION_SYSTEM_PROMPT},
                    {"role": "user", "content": request_sample},
                ],
                tier="cheap",
                stage="language_detect",
            )
            raw_code = data.get("language") if isinstance(data, dict) else ""
            candidate = str(raw_code or "").strip()
        except Exception as exc:
            # 客户端抛出的任何消息都不直通稳定错误，只通过 cause 留给调试器。
            raise LanguageDetectionUnavailable("源语言检测服务暂时不可用") from exc
        if not candidate:
            raise InvalidLanguageDetection("模型未返回明确的源语言代码")
        return candidate


def normalize_language_candidate(code: str) -> str:
    """按旧编排器规则把模型语言名称或别名规整为两字符候选。"""
    normalized = (code or "").strip().lower()
    if not normalized or normalized in _UNRESOLVED_CANDIDATES:
        return ""
    if normalized in _LANGUAGE_CANDIDATE_ALIASES:
        return _LANGUAGE_CANDIDATE_ALIASES[normalized]
    return normalized[:2] if normalized[:2].isalpha() else ""


def _detection_sample(sample: str) -> str:
    """复制实际发送的样本窗口，并在模型调用前拒绝空正文。"""
    if type(sample) is not str:
        raise TypeError("plain_sample 必须是字符串")
    request_sample = sample[:_DETECTION_SAMPLE_CHARS]
    if not request_sample.strip():
        raise EmptyLanguageSample("没有可供源语言检测的正文样本")
    return request_sample


__all__ = [
    "EmptyLanguageSample",
    "InvalidLanguageDetection",
    "LanguageDetectionError",
    "LanguageDetectionUnavailable",
    "LanguageJsonCompletion",
    "ModelSourceLanguageDetector",
    "SourceLanguageDetector",
    "normalize_language_candidate",
]
