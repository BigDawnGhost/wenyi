"""与编排框架、存储实现和用户界面无关的领域契约。"""

from .workflow import (
    ArtifactRef,
    FailureInfo,
    StageStatus,
    WorkflowEvent,
    WorkflowPhase,
    WorkflowStatus,
    build_workflow_id,
    copy_json_value,
    normalize_language_code,
    validate_artifact_ref,
    validate_failure_info,
    validate_json_value,
    validate_operation_id,
    validate_sha256,
    validate_workflow_event,
)

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
