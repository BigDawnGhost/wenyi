"""工作流领域值对象的身份稳定性和序列化边界测试。"""

from __future__ import annotations

import json

import pytest

from trans_novel.domain.workflow import (
    StageStatus,
    WorkflowStatus,
    build_workflow_id,
    copy_json_value,
    normalize_language_code,
    validate_artifact_ref,
    validate_failure_info,
    validate_operation_id,
    validate_workflow_event,
)

SOURCE_HASH = "a" * 64
PROFILE_HASH = "b" * 64


def _artifact(**overrides: object) -> dict[str, object]:
    """构造最小合法产物引用，并允许单个测试覆盖待验证字段。"""
    value: dict[str, object] = {
        "uri": "artifact://source/book.epub",
        "sha256": SOURCE_HASH,
        "media_type": "application/epub+zip",
        "size_bytes": 42,
    }
    value.update(overrides)
    return value


def test_workflow_id_is_stable_and_normalizes_target_language() -> None:
    first = build_workflow_id(SOURCE_HASH, " JA_jp ", " ZH_cn ", PROFILE_HASH)
    second = build_workflow_id(SOURCE_HASH, "ja-jp", "zh-cn", PROFILE_HASH)

    assert first == second
    assert first.startswith("wf-")
    assert len(first) == 67


@pytest.mark.parametrize(
    ("source_hash", "source_lang", "target_lang", "profile_hash"),
    [
        ("c" * 64, "ja", "zh", PROFILE_HASH),
        (SOURCE_HASH, "en", "zh", PROFILE_HASH),
        (SOURCE_HASH, "ja", "en", PROFILE_HASH),
        (SOURCE_HASH, "ja", "zh", "d" * 64),
    ],
)
def test_workflow_id_changes_when_semantic_identity_changes(
    source_hash: str,
    source_lang: str,
    target_lang: str,
    profile_hash: str,
) -> None:
    baseline = build_workflow_id(SOURCE_HASH, "ja", "zh", PROFILE_HASH)

    assert build_workflow_id(source_hash, source_lang, target_lang, profile_hash) != baseline


def test_workflow_id_collapses_common_iso_language_aliases() -> None:
    """检测器返回三字母代码时，不应为同一语言创建第二个工作流。"""
    english = build_workflow_id(SOURCE_HASH, "en", "zh", PROFILE_HASH)
    japanese = build_workflow_id(SOURCE_HASH, "ja", "zh", PROFILE_HASH)

    assert build_workflow_id(SOURCE_HASH, "eng", "zho", PROFILE_HASH) == english
    assert build_workflow_id(SOURCE_HASH, "jpn", "zh", PROFILE_HASH) == japanese


@pytest.mark.parametrize(
    ("source_hash", "source_lang", "target_lang", "profile_hash"),
    [
        ("A" * 64, "ja", "zh", PROFILE_HASH),
        ("short", "ja", "zh", PROFILE_HASH),
        (SOURCE_HASH, "", "zh", PROFILE_HASH),
        (SOURCE_HASH, "auto", "zh", PROFILE_HASH),
        (SOURCE_HASH, "mixed", "zh", PROFILE_HASH),
        (SOURCE_HASH, "uncertain", "zh", PROFILE_HASH),
        (SOURCE_HASH, "多语言", "zh", PROFILE_HASH),
        (SOURCE_HASH, "未知", "zh", PROFILE_HASH),
        (SOURCE_HASH, "??", "zh", PROFILE_HASH),
        (SOURCE_HASH, "unknown", "zh", PROFILE_HASH),
        (SOURCE_HASH, "und", "zh", PROFILE_HASH),
        (SOURCE_HASH, "ja", "", PROFILE_HASH),
        (SOURCE_HASH, "ja", "auto", PROFILE_HASH),
        (SOURCE_HASH, "ja", "zh", "not-a-hash"),
    ],
)
def test_workflow_id_rejects_ambiguous_identity_inputs(
    source_hash: str,
    source_lang: str,
    target_lang: str,
    profile_hash: str,
) -> None:
    with pytest.raises(ValueError):
        build_workflow_id(source_hash, source_lang, target_lang, profile_hash)


def test_artifact_ref_returns_an_independent_json_value() -> None:
    original = _artifact()

    artifact = validate_artifact_ref(original)
    original["uri"] = "artifact://mutated"

    assert artifact["uri"] == "artifact://source/book.epub"
    assert json.loads(json.dumps(artifact)) == artifact


@pytest.mark.parametrize(
    "artifact",
    [
        _artifact(uri=""),
        _artifact(sha256="A" * 64),
        _artifact(media_type=""),
        _artifact(size_bytes=-1),
        _artifact(size_bytes=True),
        {**_artifact(), "extra": "field"},
    ],
)
def test_artifact_ref_rejects_invalid_or_extensible_shapes(
    artifact: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        validate_artifact_ref(artifact)


def test_failure_and_event_payloads_must_be_json_serializable() -> None:
    with pytest.raises(ValueError, match="JSON"):
        validate_failure_info(
            {
                "code": "io_error",
                "message": "failed",
                "retryable": True,
                "details": {"handle": object()},
            }
        )

    with pytest.raises(ValueError, match="JSON"):
        validate_workflow_event(
            {
                "event_id": "event-1",
                "event_type": "workflow.started",
                "payload": {"value": float("nan")},
            }
        )


def test_public_value_validators_reject_strings_that_cannot_be_utf8_encoded() -> None:
    """公开验证器本身就应拒绝无法持久化的孤立 surrogate。"""
    surrogate = chr(0xD800)

    with pytest.raises(ValueError, match="UTF-8"):
        validate_artifact_ref(_artifact(uri=f"artifact://source/{surrogate}"))

    with pytest.raises(ValueError, match="UTF-8"):
        validate_failure_info(
            {
                "code": "io_error",
                "message": surrogate,
                "retryable": False,
                "details": {},
            }
        )

    with pytest.raises(ValueError, match="UTF-8"):
        validate_workflow_event(
            {
                "event_id": "event-utf8",
                "event_type": surrogate,
                "payload": {},
            }
        )


def test_stable_json_rejects_mapping_keys_that_cannot_be_utf8_encoded() -> None:
    """JSON 对象键与普通字符串值必须遵守同一 UTF-8 持久化边界。"""
    surrogate = chr(0xD800)

    with pytest.raises(ValueError, match="映射键.*UTF-8"):
        copy_json_value({surrogate: "value"})

    with pytest.raises(ValueError, match="映射键.*UTF-8"):
        validate_failure_info(
            {
                "code": "io_error",
                "message": "failed",
                "retryable": False,
                "details": {surrogate: "value"},
            }
        )

    with pytest.raises(ValueError, match="映射键.*UTF-8"):
        validate_workflow_event(
            {
                "event_id": "event-key-utf8",
                "event_type": "workflow.failed",
                "payload": {surrogate: "value"},
            }
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" JA_jp ", "ja-jp"),
        ("zh_Hans-CN", "zh-hans-cn"),
        ("eng", "en"),
        ("jpn-JP", "ja-jp"),
    ],
)
def test_language_codes_use_one_canonicalizer(raw: str, expected: str) -> None:
    """身份工厂与恢复校验共享同一套 BCP-47 风格规范化规则。"""
    assert normalize_language_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "j",
        "english!",
        "mixed-language",
        "und-latn",
        "mul-latn",
        "unk-latn",
        "zxx",
        "mis",
        "123",
        "zh--cn",
    ],
)
def test_language_codes_reject_unresolved_or_malformed_values(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_language_code(raw)


def test_public_validators_reject_scalar_subclasses() -> None:
    """公开验证器返回的值必须与稳定 JSON 边界保持一致。"""

    class StringSubclass(str):
        """模拟可能夹带自定义行为的字符串子类。"""

    class IntegerSubclass(int):
        """模拟 JSON 边界不接受的整数子类。"""

    with pytest.raises(ValueError):
        validate_operation_id(StringSubclass("prepare:start"))
    with pytest.raises(ValueError):
        validate_artifact_ref(_artifact(sha256=StringSubclass(SOURCE_HASH)))
    with pytest.raises(ValueError):
        validate_artifact_ref(_artifact(size_bytes=IntegerSubclass(42)))


@pytest.mark.parametrize(
    "value",
    [
        {"tuple": (1, 2)},
        {1: "integer-key"},
        {"set": {1, 2}},
        {"infinite": float("inf")},
        {"surrogate": chr(0xD800)},
    ],
)
def test_stable_json_values_reject_round_trip_shape_changes(value: object) -> None:
    with pytest.raises(ValueError, match="JSON"):
        copy_json_value(value)


def test_event_payload_is_copied_as_an_independent_stable_json_value() -> None:
    payload = {"chapters": [1, 2]}

    event = validate_workflow_event(
        {
            "event_id": "translate:chapters-completed",
            "event_type": "translation.completed",
            "payload": payload,
        }
    )
    payload["chapters"].append(3)

    assert event["payload"] == {"chapters": [1, 2]}


@pytest.mark.parametrize(
    "operation_id",
    ["prepare:start", "translation/chapter-12", "review.round_2", "A" * 200],
)
def test_operation_id_accepts_stable_path_safe_identifiers(operation_id: str) -> None:
    assert validate_operation_id(operation_id) == operation_id


@pytest.mark.parametrize("operation_id", ["", " has-space", "bad operation", "x" * 201])
def test_operation_id_rejects_ambiguous_or_unsafe_identifiers(operation_id: str) -> None:
    with pytest.raises(ValueError, match="operation_id"):
        validate_operation_id(operation_id)


def test_string_enums_remain_plain_json_values_on_python_310() -> None:
    payload = {
        "workflow": WorkflowStatus.RUNNING,
        "stage": StageStatus.COMPLETED,
    }

    assert WorkflowStatus.RUNNING == "running"
    assert json.loads(json.dumps(payload)) == {
        "workflow": "running",
        "stage": "completed",
    }
