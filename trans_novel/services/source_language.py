"""在工作流创建前解析明确、稳定的源语言身份。

源语言参与 ``workflow_id``，因此 ``auto`` 检测必须发生在状态工厂之前。本模块
把旧模型输出兼容规则、窄 JSON 调用端口和准入决策分开；它不依赖 Config、
具体 LLMClient、RunStore、ArtifactStore 或图运行时。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal, Protocol

from ..domain.workflow import normalize_language_code

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


class SameSourceAndTargetLanguage(ValueError):
    """源语言与目标语言主标签相同，工作流无需翻译。"""

    code = "same_source_and_target_language"
    retryable = False

    def __init__(self, language: str) -> None:
        """保存规范主标签并产生与旧 CLI 一致的用户提示。"""
        self.language = language
        super().__init__(
            f"源语言与目标语言相同（{language}），无需翻译；"
            "请修改 config.yaml 中的 language.source 或 language.target。"
        )


@dataclass(frozen=True, slots=True)
class LanguageResolution:
    """可直接用于创建工作流的规范语言对及其解析来源。"""

    source_lang: str
    target_lang: str
    method: Literal["configured", "detected"]
    sample_sha256: str | None


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


def resolve_source_language(
    *,
    configured_source_lang: str | None,
    target_lang: str,
    plain_sample: str,
    detector: SourceLanguageDetector,
) -> LanguageResolution:
    """在创建 WorkflowState 前解析语言，并拒绝同主语言翻译。

    明确配置不调用 detector；``auto``、空字符串或 ``None`` 才使用纯源文样本。
    检测样本摘要只覆盖实际发送的前 1500 个字符，可供未来 admission receipt 审计。
    """
    target = normalize_language_code(target_lang, field="target_lang")
    configured = _configured_source_value(configured_source_lang)

    if configured and configured.lower() != "auto":
        source = normalize_language_code(configured, field="source_lang")
        method: Literal["configured", "detected"] = "configured"
        sample_sha256 = None
    else:
        request_sample = _detection_sample(plain_sample)
        source = _normalize_detected_identity(detector.detect(request_sample))
        method = "detected"
        sample_sha256 = hashlib.sha256(request_sample.encode("utf-8")).hexdigest()

    source_primary = source.partition("-")[0]
    if source_primary == target.partition("-")[0]:
        raise SameSourceAndTargetLanguage(source_primary)
    return LanguageResolution(
        source_lang=source,
        target_lang=target,
        method=method,
        sample_sha256=sample_sha256,
    )


def _configured_source_value(value: str | None) -> str:
    """规范准入配置外形，但保留显式代码给领域规范化器处理。"""
    if value is None:
        return ""
    if type(value) is not str:
        raise TypeError("configured_source_lang 必须是字符串或 None")
    return value.strip()


def _normalize_detected_identity(raw_code: str) -> str:
    """映射已知语言名称后严格校验原始候选，禁止截断未解析哨兵。"""
    if type(raw_code) is not str:
        raise InvalidLanguageDetection("模型返回的源语言代码无法持久化")
    normalized = raw_code.strip().lower().replace("_", "-")
    candidate = _LANGUAGE_CANDIDATE_ALIASES.get(normalized, normalized)
    try:
        return normalize_language_code(candidate, field="source_lang")
    except ValueError as exc:
        raise InvalidLanguageDetection("模型返回的源语言代码无法持久化") from exc


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
    "LanguageResolution",
    "ModelSourceLanguageDetector",
    "SameSourceAndTargetLanguage",
    "SourceLanguageDetector",
    "normalize_language_candidate",
    "resolve_source_language",
]
