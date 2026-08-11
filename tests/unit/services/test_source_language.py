"""源语言候选兼容、模型检测与工作流创建前准入合同。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pytest

from trans_novel.services import (
    EmptyLanguageSample,
    InvalidLanguageDetection,
    LanguageDetectionError,
    LanguageDetectionUnavailable,
    ModelSourceLanguageDetector,
    SameSourceAndTargetLanguage,
    normalize_language_candidate,
    resolve_source_language,
)


@dataclass
class _JsonClient:
    """记录窄 complete_json 调用并返回固定值或异常。"""

    response: object
    calls: list[dict[str, object]] = field(default_factory=list)

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> object:
        """保存调用形状；Exception 响应用于模拟 provider 失败。"""
        self.calls.append(
            {
                "messages": messages,
                "tier": tier,
                "max_tokens": max_tokens,
                "stage": stage,
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@dataclass
class _Detector:
    """记录准入层实际交给 detector 的截断样本。"""

    result: str
    calls: list[str] = field(default_factory=list)

    def detect(self, sample: str) -> str:
        """返回固定候选。"""
        self.calls.append(sample)
        return self.result


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Japanese", "ja"),
        (" 日语 ", "ja"),
        ("RU", "ru"),
        ("russian", "ru"),
        ("fr", "fr"),
        ("unknown", ""),
        ("mixed", ""),
        ("", ""),
        ("xyz", "xy"),
        ("1en", ""),
    ],
)
def test_language_candidate_normalization_preserves_legacy_rules(
    raw: str,
    expected: str,
) -> None:
    """兼容包装必须保持旧模型别名、未解析哨兵和两字符回退。"""
    assert normalize_language_candidate(raw) == expected


def test_model_detector_preserves_prompt_tier_stage_and_sample_window() -> None:
    """检测器只发送前 1500 字纯源文，并保持旧 cheap/stage 路由。"""
    client = _JsonClient({"language": "Russian"})
    detector = ModelSourceLanguageDetector(client)

    result = detector.detect("x" * 1600)

    assert result == "Russian"
    assert len(client.calls) == 1
    call = client.calls[0]
    messages = call["messages"]
    assert isinstance(messages, list)
    assert "语言识别器" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "x" * 1500}
    assert call["tier"] == "cheap"
    assert call["stage"] == "language_detect"


def test_model_detector_rejects_empty_sample_before_model_call() -> None:
    """空白正文是不可重试的输入失败，不能产生模型费用。"""
    client = _JsonClient({"language": "ja"})

    with pytest.raises(EmptyLanguageSample) as raised:
        ModelSourceLanguageDetector(client).detect("  \n")

    assert raised.value.retryable is False
    assert client.calls == []


@pytest.mark.parametrize(
    "provider_error",
    [
        RuntimeError("API_KEY=secret source-body"),
        LanguageDetectionError("API_KEY=secret source-body"),
    ],
)
def test_model_detector_wraps_provider_failure_without_secret_in_message(
    provider_error: Exception,
) -> None:
    """任何 client 异常只留在 cause，不能借同类异常绕过稳定消息。"""
    detector = ModelSourceLanguageDetector(_JsonClient(provider_error))

    with pytest.raises(LanguageDetectionUnavailable) as raised:
        detector.detect("safe sample")

    assert raised.value.retryable is True
    assert raised.value.__cause__ is provider_error
    assert "secret" not in str(raised.value)
    assert "source-body" not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [None, [], {}, {"language": ""}, {"other": "ja"}],
)
def test_model_detector_rejects_missing_or_empty_output(response: object) -> None:
    """非对象、缺字段和空字段不能产生模型候选。"""
    with pytest.raises(InvalidLanguageDetection):
        ModelSourceLanguageDetector(_JsonClient(response)).detect("sample")


def test_explicit_resolution_is_canonical_and_skips_detector() -> None:
    """明确源语言直接规范化，既不读取样本也不调用模型。"""
    detector = _Detector("should-not-be-used")

    resolution = resolve_source_language(
        configured_source_lang="ENG_us",
        target_lang="ZH_hans",
        plain_sample="",
        detector=detector,
    )

    assert resolution.source_lang == "en-us"
    assert resolution.target_lang == "zh-hans"
    assert resolution.method == "configured"
    assert resolution.sample_sha256 is None
    assert detector.calls == []


def test_auto_resolution_records_digest_of_exact_detection_window() -> None:
    """自动准入只散列实际送检窗口，使未来 receipt 可稳定重放。"""
    detector = _Detector("Japanese")
    sample = "a" * 1600

    resolution = resolve_source_language(
        configured_source_lang="auto",
        target_lang="zh",
        plain_sample=sample,
        detector=detector,
    )

    request_sample = "a" * 1500
    assert resolution.source_lang == "ja"
    assert resolution.target_lang == "zh"
    assert resolution.method == "detected"
    assert resolution.sample_sha256 == hashlib.sha256(request_sample.encode()).hexdigest()
    assert detector.calls == [request_sample]


@pytest.mark.parametrize(
    ("configured", "target", "detected"),
    [("ja", "ja-JP", "unused"), ("auto", "zh-Hans", "chinese")],
)
def test_resolution_rejects_matching_primary_languages(
    configured: str,
    target: str,
    detected: str,
) -> None:
    """语言地区变体共享主标签时仍无需翻译。"""
    with pytest.raises(SameSourceAndTargetLanguage) as raised:
        resolve_source_language(
            configured_source_lang=configured,
            target_lang=target,
            plain_sample="sample",
            detector=_Detector(detected),
        )

    assert raised.value.language == target.split("-", 1)[0]


def test_auto_resolution_rejects_non_persistable_detector_candidate() -> None:
    """旧候选规范化可兼容宽松模型输出，但准入必须再通过领域代码校验。"""
    with pytest.raises(InvalidLanguageDetection):
        resolve_source_language(
            configured_source_lang=None,
            target_lang="zh",
            plain_sample="sample",
            detector=_Detector("日本語"),
        )


@pytest.mark.parametrize(
    "unresolved",
    [
        "mixed-language",
        "mul",
        "multiple",
        "multilingual",
        "unk",
        "unknown",
        "zxx",
        "mis",
    ],
)
def test_auto_resolution_rejects_unresolved_identity_before_legacy_truncation(
    unresolved: str,
) -> None:
    """不确定语言必须按原值拒绝，不能先截成看似合法的两字母代码。"""
    with pytest.raises(InvalidLanguageDetection):
        resolve_source_language(
            configured_source_lang="auto",
            target_lang="zh",
            plain_sample="sample",
            detector=_Detector(unresolved),
        )
