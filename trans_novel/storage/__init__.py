"""工作流产物与状态的具体存储适配器。

该包只实现 ``workflow.repository`` 声明的持久化端口，不负责旧 ``RunStore``
兼容、CLI 接入或执行引擎选择。
"""

from ..workflow.repository import (
    ArtifactCorruption,
    ArtifactNotFound,
    ArtifactStoreError,
    InvalidArtifactReference,
)
from .content_addressed_artifacts import ContentAddressedArtifactStore
from .sqlite_workflows import SQLiteWorkflowRepository

__all__ = [
    "ArtifactCorruption",
    "ArtifactNotFound",
    "ArtifactStoreError",
    "ContentAddressedArtifactStore",
    "InvalidArtifactReference",
    "SQLiteWorkflowRepository",
]
