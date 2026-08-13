"""新版 preparation phase 的权威提交、崩溃边界与依赖隔离测试。"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import cast

import pytest

from trans_novel.application.runtime import ExecutionContext
from trans_novel.application.workflow_preparation import (
    PreparationFailure,
    PreparationPhaseRunner,
    SourceNormalizationResult,
)
from trans_novel.domain.normalized_document import (
    NORMALIZED_DOCUMENT_MEDIA_TYPE,
    NormalizedDocumentV1,
    decode_normalized_document_v1,
)
from trans_novel.domain.workflow import StageStatus, WorkflowPhase, WorkflowStatus
from trans_novel.storage.content_addressed_artifacts import ContentAddressedArtifactStore
from trans_novel.storage.sqlite_workflows import SQLiteWorkflowRepository
from trans_novel.workflow import OperationConflict, StatePatch, new_workflow_state
from trans_novel.workflow.repository import ArtifactNotFound, ArtifactStore, WorkflowRepository

_PROFILE_HASH = "b" * 64


def _document(source_sha256: str) -> NormalizedDocumentV1:
    """构造包含一个正文段的最小、严格 V1 文档。"""
    return {
        "schema_version": 1,
        "source_sha256": source_sha256,
        "source_format": "text",
        "source_lang": "ja",
        "title": "雪国",
        "chapters": [
            {
                "index": 0,
                "title": "第一章",
                "segments": [
                    {
                        "index": 0,
                        "source": "国境の長いトンネルを抜けると雪国であった。",
                        "kind": "text",
                        "anchor": None,
                        "resource_href": None,
                        "cont": False,
                        "meta": {},
                    }
                ],
                "href": None,
                "template": None,
                "meta": {"heading_level": 1},
            }
        ],
        "meta": {},
    }


@dataclass
class _Normalizer:
    """返回确定性结果并记录 runner 注入的稳定身份。"""

    result: SourceNormalizationResult
    calls: int = 0

    def normalize(self, **kwargs: object) -> SourceNormalizationResult:
        self.calls += 1
        assert kwargs["source_sha256"] == self.result.document["source_sha256"]
        assert kwargs["source_format"] == "text"
        assert kwargs["source_lang"] == "ja"
        return self.result


class _TypedFailureNormalizer:
    """抛出仅含稳定公开字段的预期业务失败。"""

    def normalize(self, **kwargs: object) -> SourceNormalizationResult:
        del kwargs
        raise PreparationFailure(
            "reader_unavailable",
            "source reader is temporarily unavailable",
            retryable=True,
            details={"reader_family": "text"},
        )


class _ProgrammingFailureNormalizer:
    """模拟必须向上暴露、不得固化为业务失败的适配器缺陷。"""

    def normalize(self, **kwargs: object) -> SourceNormalizationResult:
        del kwargs
        raise AssertionError("secret parser invariant")


@dataclass
class _MismatchNormalizer:
    """返回与 workflow request 不同的 document identity。"""

    normalized_source: dict[str, object]
    document: NormalizedDocumentV1

    def normalize(self, **kwargs: object) -> SourceNormalizationResult:
        del kwargs
        return SourceNormalizationResult(
            normalized_source=cast(dict, self.normalized_source),
            document=self.document,
        )


class _RepositoryConflictProxy:
    """让 complete CAS 失败，同时保留真实仓储的读取和 start 提交。"""

    def __init__(self, delegate: SQLiteWorkflowRepository) -> None:
        self._delegate = delegate

    def get(self, workflow_id: str):
        return self._delegate.get(workflow_id)

    def commit_patch(self, workflow_id: str, patch: StatePatch):
        if patch.operation_id == "prepare:complete":
            raise OperationConflict("simulated concurrent normalization conflict")
        return self._delegate.commit_patch(workflow_id, patch)


class _MissingSourceStore:
    """仅在 source verify 处注入带敏感路径的 missing 错误。"""

    def verify(self, ref: dict[str, object]):
        del ref
        raise ArtifactNotFound(r"missing C:\secret\private-book.txt")


def _new_runtime(tmp_path):
    """发布原始 source，并创建真实 CAS 与 SQLite 权威仓储。"""
    artifacts = ContentAddressedArtifactStore(tmp_path / "artifacts")
    source = artifacts.put_bytes("雪国".encode(), media_type="text/plain; charset=utf-8")
    state = new_workflow_state(
        source_artifact=source,
        source_format="text",
        source_lang="ja",
        target_lang="zh",
        semantic_profile_hash=_PROFILE_HASH,
    )
    repository = SQLiteWorkflowRepository(tmp_path / "workflow.sqlite3")
    repository.create(state)
    return artifacts, repository, state


def test_pending_preparation_commits_start_cold_document_and_complete(tmp_path) -> None:
    """一次调用跨过 start 与 complete，并只持久化冷读验证后的引用。"""
    artifacts, repository, initial = _new_runtime(tmp_path)
    normalizer = _Normalizer(
        SourceNormalizationResult(
            normalized_source=initial["request"]["source_artifact"],
            document=_document(initial["request"]["source_sha256"]),
        )
    )

    PreparationPhaseRunner(normalizer)(
        initial,
        repository=repository,
        artifacts=artifacts,
        context=ExecutionContext(run_id="prepare-success"),
    )

    committed = repository.get(initial["workflow_id"])
    assert committed["revision"] == 2
    assert committed["status"] == WorkflowStatus.RUNNING.value
    assert committed["cursor"]["phase"] == WorkflowPhase.UNDERSTAND.value
    assert committed["preparation"] == {
        "status": StageStatus.COMPLETED.value,
        "normalized_source": initial["request"]["source_artifact"],
    }
    assert committed["book"]["chapter_count"] == 1
    assert committed["book"]["source_segment_count"] == 1
    document_ref = committed["book"]["document_artifact"]
    assert document_ref is not None
    assert document_ref["media_type"] == NORMALIZED_DOCUMENT_MEDIA_TYPE
    with artifacts.open_binary(artifacts.verify(document_ref)) as reader:
        assert decode_normalized_document_v1(reader.read()) == normalizer.result.document
    assert normalizer.calls == 1


def test_running_preparation_resumes_without_replaying_start(tmp_path) -> None:
    """start 已提交时只做剩余工作，并保持 operation ledger 各一条。"""
    artifacts, repository, initial = _new_runtime(tmp_path)
    repository.commit_patch(
        initial["workflow_id"],
        StatePatch(
            operation_id="prepare:start",
            expected_revision=0,
            updates={
                "status": WorkflowStatus.RUNNING.value,
                "preparation": {
                    "status": StageStatus.RUNNING.value,
                    "normalized_source": None,
                },
            },
            events=(
                {
                    "event_id": "prepare-started",
                    "event_type": "preparation.started",
                    "payload": {"phase": "prepare"},
                },
            ),
        ),
    )
    running = repository.get(initial["workflow_id"])
    normalizer = _Normalizer(
        SourceNormalizationResult(
            normalized_source=initial["request"]["source_artifact"],
            document=_document(initial["request"]["source_sha256"]),
        )
    )

    PreparationPhaseRunner(normalizer)(
        running,
        repository=repository,
        artifacts=artifacts,
        context=ExecutionContext(run_id="prepare-resume"),
    )

    committed = repository.get(initial["workflow_id"])
    assert set(committed["applied_operations"]) == {"prepare:start", "prepare:complete"}
    assert committed["revision"] == 2


def test_concurrent_winner_that_left_prepare_is_a_successful_noop(tmp_path) -> None:
    """后到执行者重放 start 后看到下一阶段时不重复解析。"""
    artifacts, repository, initial = _new_runtime(tmp_path)
    first = _Normalizer(
        SourceNormalizationResult(
            normalized_source=initial["request"]["source_artifact"],
            document=_document(initial["request"]["source_sha256"]),
        )
    )
    PreparationPhaseRunner(first)(
        initial,
        repository=repository,
        artifacts=artifacts,
        context=ExecutionContext(run_id="winner"),
    )
    late = _ProgrammingFailureNormalizer()

    PreparationPhaseRunner(late)(
        initial,
        repository=repository,
        artifacts=artifacts,
        context=ExecutionContext(run_id="late-worker"),
    )

    assert repository.get(initial["workflow_id"])["revision"] == 2


def test_typed_failure_is_sanitized_and_committed_without_artifacts(tmp_path) -> None:
    """显式失败写入规范 lifecycle event，绝不持久化 cause 或路径文本。"""
    artifacts, repository, initial = _new_runtime(tmp_path)

    PreparationPhaseRunner(_TypedFailureNormalizer())(
        initial,
        repository=repository,
        artifacts=artifacts,
        context=ExecutionContext(run_id="typed-failure"),
    )

    failed = repository.get(initial["workflow_id"])
    assert failed["status"] == WorkflowStatus.FAILED.value
    assert failed["cursor"]["phase"] == WorkflowPhase.PREPARE.value
    assert failed["book"]["document_artifact"] is None
    assert failed["failure"] == {
        "code": "reader_unavailable",
        "message": "source reader is temporarily unavailable",
        "retryable": True,
        "details": {"reader_family": "text"},
    }
    assert "secret" not in repr(failed)


def test_unknown_normalizer_exception_propagates_without_durable_failure(tmp_path) -> None:
    """编程错误保持可见；仓储只保留已成功提交的 start 恢复事实。"""
    artifacts, repository, initial = _new_runtime(tmp_path)

    with pytest.raises(AssertionError, match="secret parser invariant"):
        PreparationPhaseRunner(_ProgrammingFailureNormalizer())(
            initial,
            repository=repository,
            artifacts=artifacts,
            context=ExecutionContext(run_id="programming-error"),
        )

    running = repository.get(initial["workflow_id"])
    assert running["revision"] == 1
    assert running["status"] == WorkflowStatus.RUNNING.value
    assert running["failure"] is None


def test_invalid_document_identity_fails_before_publishing_document(tmp_path) -> None:
    """hash/format/lang 绑定不一致时只提交安全失败，不产生 document 引用。"""
    artifacts, repository, initial = _new_runtime(tmp_path)
    document = _document("c" * 64)
    runner = PreparationPhaseRunner(
        _MismatchNormalizer(
            normalized_source=initial["request"]["source_artifact"],
            document=document,
        )
    )
    runner(
        initial,
        repository=repository,
        artifacts=artifacts,
        context=ExecutionContext(run_id="identity-mismatch"),
    )

    failed = repository.get(initial["workflow_id"])
    assert failed["failure"]["code"] == "normalized_document_identity_mismatch"
    assert failed["book"]["document_artifact"] is None


def test_repository_conflict_during_complete_propagates_without_failure_patch(tmp_path) -> None:
    """complete 的仓储并发冲突必须向上暴露，不能改写成业务失败。"""
    artifacts, repository, initial = _new_runtime(tmp_path)
    normalizer = _Normalizer(
        SourceNormalizationResult(
            normalized_source=initial["request"]["source_artifact"],
            document=_document(initial["request"]["source_sha256"]),
        )
    )
    proxy = _RepositoryConflictProxy(repository)

    with pytest.raises(OperationConflict, match="simulated concurrent"):
        PreparationPhaseRunner(normalizer)(
            initial,
            repository=cast(WorkflowRepository, proxy),
            artifacts=artifacts,
            context=ExecutionContext(run_id="complete-conflict"),
        )

    current = repository.get(initial["workflow_id"])
    assert current["revision"] == 1
    assert current["status"] == WorkflowStatus.RUNNING.value
    assert current["failure"] is None


def test_artifact_not_found_is_safely_categorized_without_path_leak(tmp_path) -> None:
    """CAS 异常只按类型持久化，异常消息中的本地路径不会进入状态。"""
    _, repository, initial = _new_runtime(tmp_path)

    PreparationPhaseRunner(_ProgrammingFailureNormalizer())(
        initial,
        repository=repository,
        artifacts=cast(ArtifactStore, _MissingSourceStore()),
        context=ExecutionContext(run_id="missing-source"),
    )

    failed = repository.get(initial["workflow_id"])
    assert failed["failure"]["code"] == "source_artifact_not_found"
    assert "secret" not in repr(failed)
    assert failed["book"]["document_artifact"] is None


def test_clean_import_does_not_load_legacy_graph_or_concrete_storage() -> None:
    """纯 application runner 的 import 不得拉入任一运行时实现。"""
    script = """
import sys
import trans_novel.application.workflow_preparation

forbidden = (
    "langgraph",
    "trans_novel.cli",
    "trans_novel.config",
    "trans_novel.graph",
    "trans_novel.ingest",
    "trans_novel.llm",
    "trans_novel.pipeline",
    "trans_novel.storage",
    "pydantic",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected preparation dependencies: {loaded}")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
