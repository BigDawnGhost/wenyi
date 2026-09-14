"""V4 typed resumable workflow state."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from trans_novel.ingest.models import ChapterProcessing

RUN_STATE_SCHEMA_VERSION = 4
RUN_INPUT_SCHEMA_VERSION = 2
TRANSLATION_POLICY_VERSION = 2
STATUS_PENDING = "pending"
STATUS_DONE = "done"
ChapterStatus = Literal["pending", "done"]
NODE_PENDING = "pending"
NODE_RUNNING = "running"
NODE_SUCCEEDED = "succeeded"
NODE_SKIPPED = "skipped"
NODE_FAILED_RETRYABLE = "failed_retryable"
NODE_FAILED_PERMANENT = "failed_permanent"
NodeStatus = Literal[
    "pending", "running", "succeeded", "skipped", "failed_retryable", "failed_permanent"
]
NODE_PREPARE = "prepare"
NODE_ANALYZE = "analyze"
NODE_LAYOUT = "layout"
NODE_MINE_TERMS = "mine_terms"
NODE_NAME_TERMS = "name_terms"
NODE_TRANSLATE = "translate"
NODE_POLISH = "polish"
NODE_TITLES = "titles"
NODE_DETERMINISTIC_QA = "deterministic_qa"
NODE_REPAIR = "repair"
NODE_REPORT = "report"
NODE_ASSEMBLE = "assemble"
SCOPE_BOOK = "book"
SCOPE_CHAPTER = "chapter"
BEST_EFFORT_NODES = frozenset({NODE_MINE_TERMS, NODE_NAME_TERMS})

_NODE_DESCENDANTS: dict[str, tuple[tuple[str, bool | str], ...]] = {
    NODE_LAYOUT: ((NODE_ASSEMBLE, False),),
    NODE_MINE_TERMS: ((NODE_NAME_TERMS, False),),
    NODE_NAME_TERMS: ((NODE_TRANSLATE, "all"),),
    NODE_TRANSLATE: ((NODE_POLISH, True),),
    NODE_POLISH: ((NODE_TITLES, False),),
    NODE_TITLES: ((NODE_DETERMINISTIC_QA, False),),
    NODE_DETERMINISTIC_QA: ((NODE_REPAIR, False),),
    NODE_REPAIR: ((NODE_REPORT, False),),
    NODE_REPORT: ((NODE_ASSEMBLE, False),),
    NODE_ASSEMBLE: (),
}


class IdentityMismatchError(RuntimeError):
    """Stored run identity does not match the current source."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def source_bytes_hash(path: str) -> str:
    try:
        with open(path, "rb") as stream:
            return hashlib.sha256(stream.read()).hexdigest()
    except OSError:
        return ""


def stable_digest(payload: object) -> str:
    """返回持久化状态日志所用规范 JSON 的 SHA-256 哈希值。"""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def input_fingerprint(*parts: object) -> str:
    payload: list[Any] = []
    for part in parts:
        if isinstance(part, BaseModel):
            payload.append(part.model_dump(mode="json"))
        elif isinstance(part, list | tuple):
            payload.append(list(part))
        else:
            payload.append(part)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def chapter_node_key(node_id: str, ci: int) -> str:
    return f"{node_id}:{ci}"


class RunIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source_bytes_sha256: str = ""
    run_input_schema_version: int = RUN_INPUT_SCHEMA_VERSION
    translation_policy_version: int = 0
    source_lang: str = ""
    target_lang: str = ""


class NodeFailure(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["provider_retryable", "provider_permanent", "protocol", "business", "interrupted"]
    message: str = ""
    at: str = ""


class NodeState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    node_id: str
    status: NodeStatus = NODE_PENDING
    attempts: int = 0
    input_fingerprint: str = ""
    failure: NodeFailure | None = None
    started_at: str | None = None
    finished_at: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class PolishBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: int
    count: int


class RepairIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    chapter: int
    index: int
    type: str
    detail: str = ""
    attempts: int = 0
    status: Literal["pending", "repairing", "resolved", "accepted_after_exhaustion"] = "pending"
    last_detail: str = ""
    committed_target_fingerprint: str = ""


class ChapterProgress(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: ChapterStatus = STATUS_PENDING
    pending_polish: list[PolishBatch] = Field(default_factory=list)
    lint_issues: list[dict[str, Any]] = Field(default_factory=list)
    repair_ledger: dict[str, RepairIssue] = Field(default_factory=dict)
    back_matter_mode: str | None = None


class ChapterIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    title: str = ""
    href: str | None = None
    toc_entry_id: str | None = None
    title_translated: str | None = None
    processing: ChapterProcessing | None = None


class AnalysisFlags(BaseModel):
    model_config = ConfigDict(extra="ignore")
    term_mining_done: bool = False


class RunState(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run_state_schema: int = RUN_STATE_SCHEMA_VERSION
    identity: RunIdentity = Field(default_factory=RunIdentity)
    title: str = ""
    fmt: str = ""
    source_path: str = ""
    source_lang: str = ""
    target_lang: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    initialized: bool = False
    chapters: list[ChapterIndex] = Field(default_factory=list)
    progress: dict[int, ChapterProgress] = Field(default_factory=dict)
    nodes: dict[str, NodeState] = Field(default_factory=dict)
    analysis_flags: AnalysisFlags = Field(default_factory=AnalysisFlags)

    def reconcile_fingerprints(self, computed: dict[str, str]) -> set[str]:
        invalidated = {
            key
            for key, fingerprint in computed.items()
            if (node := self.nodes.get(key)) is not None
            and node.input_fingerprint
            and node.input_fingerprint != fingerprint
        }
        expanded = set(invalidated)
        queue = list(invalidated)
        while queue:
            key = queue.pop()
            base, sep, suffix = key.partition(":")
            for child, mode in _NODE_DESCENDANTS.get(base, ()):
                if mode == "all":
                    children = [f"{child}:{c.index}" for c in self.chapters]
                elif mode is True:
                    children = [f"{child}:{suffix}"] if sep else []
                else:
                    children = [child]
                for child_key in children:
                    if child_key not in expanded:
                        expanded.add(child_key)
                        queue.append(child_key)
        for key in expanded:
            self.nodes[key] = NodeState(node_id=key)
            self._clear_node_artifact(key)
        return expanded

    def _clear_node_artifact(self, key: str) -> None:
        base, sep, suffix = key.partition(":")
        progress = self.progress.get(int(suffix)) if sep and suffix.isdigit() else None
        if progress is None:
            return
        if base == NODE_TRANSLATE:
            progress.pending_polish = []
            progress.lint_issues = []
        elif base == NODE_POLISH:
            progress.pending_polish = []

    def recover_interrupted(self) -> bool:
        changed = False
        for node in self.nodes.values():
            if node.status == NODE_RUNNING:
                node.status = NODE_PENDING
                node.failure = NodeFailure(kind="interrupted", message="进程中断", at=now_iso())
                changed = True
        for progress in self.progress.values():
            for issue in progress.repair_ledger.values():
                if issue.status == "repairing":
                    issue.status = (
                        "accepted_after_exhaustion" if issue.attempts >= 10 else "pending"
                    )
                    issue.last_detail = "进程中断，保留已消耗的 Repair 预算"
                    changed = True
        return changed
