"""工作流领域值对象。

本模块只定义可序列化、与执行框架无关的稳定契约。大型正文、分析结果和导出
文件都通过 ``ArtifactRef`` 引用；运行时客户端、数据库连接和文件句柄不得进入
这些类型。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, TypedDict

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
_UNRESOLVED_LANGUAGE_CODES = frozenset(
    {
        "??",
        "auto",
        "mixed",
        "mixed-language",
        "mul",
        "multiple",
        "multilingual",
        "uncertain",
        "und",
        "unk",
        "unknown",
        "zxx",
        "mis",
        "多语言",
        "未知",
    }
)

# 当前产品文档使用 ISO 639-1。将常见的三字母检测结果收敛到同一身份，
# 避免 ``eng``/``en`` 这类等价输入生成两个 workflow_id。
_LANGUAGE_PRIMARY_ALIASES = {
    "deu": "de",
    "eng": "en",
    "fra": "fr",
    "fre": "fr",
    "ger": "de",
    "ita": "it",
    "jpn": "ja",
    "kor": "ko",
    "por": "pt",
    "rus": "ru",
    "spa": "es",
    "zho": "zh",
    "jp": "ja",
}


class WorkflowStatus(str, Enum):
    """一次书籍工作流的顶层生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkflowPhase(str, Enum):
    """可持久化游标能够停留的粗粒度阶段。"""

    PREPARE = "prepare"
    UNDERSTAND = "understand"
    TRANSLATE_CHAPTERS = "translate_chapters"
    TRANSLATE_TITLES = "translate_titles"
    REVIEW = "review"
    QUALITY = "quality"
    EXPORT = "export"
    COMPLETE = "complete"


class StageStatus(str, Enum):
    """单个可选或必选阶段的执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class ArtifactRef(TypedDict):
    """指向不可变大型产物的内容寻址引用。"""

    uri: str
    sha256: str
    media_type: str
    size_bytes: int


class FailureInfo(TypedDict):
    """可安全持久化的失败摘要，不包含 traceback 或敏感请求正文。"""

    code: str
    message: str
    retryable: bool
    details: dict[str, Any]


class WorkflowEvent(TypedDict):
    """节点提交时随状态补丁发布的稳定领域事件。"""

    event_id: str
    event_type: str
    payload: dict[str, Any]


def validate_sha256(value: object, *, field: str = "sha256") -> str:
    """验证并返回小写 SHA-256；不接受会造成身份歧义的宽松格式。"""
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} 必须是 64 位小写十六进制 SHA-256")
    return value


def validate_operation_id(value: object, *, field: str = "operation_id") -> str:
    """验证可用于 checkpoint、日志和文件索引的稳定操作标识。"""
    if type(value) is not str or _OPERATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} 必须以字母或数字开头，且只含字母数字、._:/-（最多 200 字符）")
    return value


def build_workflow_id(
    source_sha256: str,
    source_lang: str,
    target_lang: str,
    semantic_profile_hash: str,
) -> str:
    """由内容、语言对和语义配置生成跨机器稳定的工作流身份。"""
    source = validate_sha256(source_sha256, field="source_sha256")
    profile = validate_sha256(
        semantic_profile_hash,
        field="semantic_profile_hash",
    )
    normalized_source = normalize_language_code(source_lang, field="source_lang")
    normalized_target = normalize_language_code(target_lang, field="target_lang")

    # 版本化前缀给未来身份算法升级留下明确迁移边界。
    identity = "\0".join(
        ("wenyi-workflow-v1", source, normalized_source, normalized_target, profile)
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"wf-{digest}"


def normalize_language_code(value: object, *, field: str = "language") -> str:
    """返回规范 BCP-47 风格代码，并拒绝自动检测或不确定语言哨兵。"""
    normalized = (
        _validate_non_empty_utf8_string(value, field=field).strip().lower().replace("_", "-")
    )
    primary, separator, suffix = normalized.partition("-")
    if normalized in _UNRESOLVED_LANGUAGE_CODES or primary in _UNRESOLVED_LANGUAGE_CODES:
        raise ValueError(f"{field} 必须是语言检测完成后的明确代码")
    primary = _LANGUAGE_PRIMARY_ALIASES.get(primary, primary)
    normalized = primary + (separator + suffix if separator else "")
    if _LANGUAGE_CODE_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field} 必须是规范的 BCP-47 风格语言代码")
    return normalized


def validate_artifact_ref(value: Mapping[str, object]) -> ArtifactRef:
    """校验产物引用并返回与调用方不共享可变对象的规范副本。"""
    expected_keys = {"uri", "sha256", "media_type", "size_bytes"}
    if set(value) != expected_keys:
        raise ValueError("ArtifactRef 字段必须恰好为 uri/sha256/media_type/size_bytes")

    uri = _validate_non_empty_utf8_string(value["uri"], field="ArtifactRef.uri")
    media_type = _validate_non_empty_utf8_string(
        value["media_type"],
        field="ArtifactRef.media_type",
    )
    size_bytes = value["size_bytes"]
    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError("ArtifactRef.size_bytes 必须是非负整数")

    return {
        "uri": uri,
        "sha256": validate_sha256(value["sha256"], field="ArtifactRef.sha256"),
        "media_type": media_type,
        "size_bytes": size_bytes,
    }


def validate_failure_info(value: Mapping[str, object]) -> FailureInfo:
    """验证失败记录只包含稳定、安全且可 JSON 序列化的字段。"""
    expected_keys = {"code", "message", "retryable", "details"}
    if set(value) != expected_keys:
        raise ValueError("FailureInfo 字段必须恰好为 code/message/retryable/details")
    code = _validate_non_empty_utf8_string(value["code"], field="FailureInfo.code")
    message = _validate_non_empty_utf8_string(value["message"], field="FailureInfo.message")
    if not isinstance(value["retryable"], bool):
        raise ValueError("FailureInfo.retryable 必须是布尔值")
    if not isinstance(value["details"], Mapping):
        raise ValueError("FailureInfo.details 必须是映射")

    details = copy_json_value(dict(value["details"]), field="FailureInfo.details")
    return {
        "code": code,
        "message": message,
        "retryable": value["retryable"],
        "details": details,
    }


def validate_workflow_event(value: Mapping[str, object]) -> WorkflowEvent:
    """验证事件标识、类型和负载，确保重放时不依赖 Python 对象。"""
    expected_keys = {"event_id", "event_type", "payload"}
    if set(value) != expected_keys:
        raise ValueError("WorkflowEvent 字段必须恰好为 event_id/event_type/payload")
    event_id = validate_operation_id(value["event_id"], field="WorkflowEvent.event_id")
    event_type = _validate_non_empty_utf8_string(
        value["event_type"],
        field="WorkflowEvent.event_type",
    )
    if not isinstance(value["payload"], Mapping):
        raise ValueError("WorkflowEvent.payload 必须是映射")

    payload = copy_json_value(dict(value["payload"]), field="WorkflowEvent.payload")
    return {
        "event_id": event_id,
        "event_type": event_type,
        "payload": payload,
    }


def copy_json_value(value: object, *, field: str = "value") -> Any:
    """复制稳定 JSON 值，并拒绝会在编码往返后改变形状的 Python 对象。"""
    if value is None or type(value) is bool:
        return value
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(f"{field} 必须是可写入 UTF-8 的稳定 JSON 字符串") from error
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{field} 的稳定 JSON 值不能包含 NaN 或 Infinity")
        return value
    if isinstance(value, list):
        return [copy_json_value(item, field=f"{field}[]") for item in value]
    if isinstance(value, dict):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{field} 的稳定 JSON 映射键必须是字符串")
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(f"{field} 的稳定 JSON 映射键必须可写入 UTF-8") from error
            copied[key] = copy_json_value(item, field=f"{field}.{key}")
        return copied
    raise ValueError(
        f"{field} 只能包含稳定 JSON 值：None/bool/int/有限 float/str/list/字符串键 dict"
    )


def validate_json_value(value: object, *, field: str = "value") -> None:
    """验证值经过 JSON 编码和解码后仍保持完全相同的结构。"""
    copied = copy_json_value(value, field=field)
    encoded = json.dumps(copied, ensure_ascii=False, sort_keys=True, allow_nan=False)
    encoded.encode("utf-8")
    if json.loads(encoded) != copied:  # pragma: no cover - 防御标准库行为变化
        raise ValueError(f"{field} 的 JSON 往返结果不稳定")


def _validate_non_empty_utf8_string(value: object, *, field: str) -> str:
    """返回原生 UTF-8 字符串，并拒绝空值、子类和孤立 surrogate。"""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} 不能为空且必须是原生字符串")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} 必须可写入 UTF-8") from error
    return value


__all__ = [
    "ArtifactRef",
    "FailureInfo",
    "StageStatus",
    "WorkflowEvent",
    "WorkflowPhase",
    "WorkflowStatus",
    "build_workflow_id",
    "copy_json_value",
    "normalize_language_code",
    "validate_artifact_ref",
    "validate_failure_info",
    "validate_json_value",
    "validate_operation_id",
    "validate_sha256",
    "validate_workflow_event",
]
