"""工作流产物的具体存储适配器。

该包只实现 ``workflow.repository`` 声明的持久化端口，不负责工作流状态提交、
旧 ``RunStore`` 兼容或执行引擎选择。
"""

from ..workflow.repository import (
    ArtifactCorruption,
    ArtifactNotFound,
    ArtifactStoreError,
    InvalidArtifactReference,
)
from .content_addressed_artifacts import ContentAddressedArtifactStore

__all__ = [
    "ArtifactCorruption",
    "ArtifactNotFound",
    "ArtifactStoreError",
    "ContentAddressedArtifactStore",
    "InvalidArtifactReference",
]
