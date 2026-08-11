"""与具体运行引擎解耦的工作流状态和纯状态转换。"""

from .factory import new_workflow_state
from .patches import (
    InvalidStatePatch,
    MergeConflict,
    OperationConflict,
    PatchApplication,
    RevisionConflict,
    StatePatch,
)
from .reducers import apply_state_patch, merge_unique_mapping
from .repository import (
    ArtifactCorruption,
    ArtifactNotFound,
    ArtifactStore,
    ArtifactStoreError,
    InvalidArtifactReference,
)
from .state import (
    ALLOWED_UPDATE_KEYS,
    OPTIONAL_STAGE_NAMES,
    REQUIRED_STAGE_NAMES,
    RESERVED_UPDATE_KEYS,
    WORKFLOW_SCHEMA_VERSION,
    WORKFLOW_STAGE_NAMES,
    WORKFLOW_STATE_KEYS,
    WorkflowState,
)
from .validation import validate_workflow_state

__all__ = [
    "ALLOWED_UPDATE_KEYS",
    "ArtifactCorruption",
    "ArtifactNotFound",
    "ArtifactStore",
    "ArtifactStoreError",
    "InvalidStatePatch",
    "InvalidArtifactReference",
    "MergeConflict",
    "OperationConflict",
    "OPTIONAL_STAGE_NAMES",
    "PatchApplication",
    "RESERVED_UPDATE_KEYS",
    "REQUIRED_STAGE_NAMES",
    "RevisionConflict",
    "StatePatch",
    "WORKFLOW_SCHEMA_VERSION",
    "WORKFLOW_STAGE_NAMES",
    "WORKFLOW_STATE_KEYS",
    "WorkflowState",
    "apply_state_patch",
    "merge_unique_mapping",
    "new_workflow_state",
    "validate_workflow_state",
]
