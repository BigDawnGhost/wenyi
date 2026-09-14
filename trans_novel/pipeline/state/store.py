"""运行态持久化：支持断点续跑与状态迁移。

目录结构（state_dir/<book-slug>/）：
  manifest.json         根状态；
  chapters_v2/ch{n}.json  各章；
  context.json          滚动上下文；
  analysis.json         全局分析产物；
  glossary.db            术语库；
  layout_work.json       EPUB 布局分析检查点；
  layout_profile.json    已接受的 EPUB 布局分析结果；
  report.json            QA 报告；
  usage.json             本书跨续跑累计的 LLM token 用量；
  events.jsonl           追加式行为日志。
"""

from __future__ import annotations

import json
import os
import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from trans_novel.ingest import Chapter, Document
from trans_novel.pipeline.state.invalidation import (
    clear_translated_titles,
    clear_translation_targets,
    reconcile_fingerprints,
)
from trans_novel.pipeline.state.lifecycle import (
    fail_node,
    mark_node_running,
    mark_node_skipped,
    mark_node_succeeded,
    record_node_fingerprint,
    record_node_output,
)
from trans_novel.pipeline.state.migration import (
    migrate_v1_to_v2,
    migrate_v2_to_v3,
    migrate_v3_to_v4,
)
from trans_novel.pipeline.state.models import (
    RUN_INPUT_SCHEMA_VERSION,
    RUN_STATE_SCHEMA_VERSION,
    STATUS_DONE,
    STATUS_PENDING,
    TRANSLATION_POLICY_VERSION,
    ChapterIndex,
    ChapterProgress,
    IdentityMismatchError,
    RunIdentity,
    RunState,
    stable_digest,
)
from trans_novel.postprocess.language import normalize_lang_code

__all__ = [
    "STATUS_DONE",
    "STATUS_PENDING",
    "RunStore",
    "slugify",
    "stable_digest",
]


def slugify(name: str) -> str:
    s = re.sub(r"[^\w一-鿿぀-ヿ-]+", "_", name).strip("_")
    return s or "book"


def _recover_note_migration(store) -> None:
    from trans_novel.pipeline.state.note_migration import recover_note_migration

    recover_note_migration(store)


def _merge_epub_verification(store, data: dict) -> dict:
    report = dict(data)
    report.pop("published_outputs", None)
    published_outputs: dict[str, dict] = {}
    if os.path.isfile(store.epub_verification_path):
        previous = store.read_json(store.epub_verification_path)
        if not isinstance(previous, dict):
            raise ValueError("invalid EPUB verification report")
        persisted = previous.get("published_outputs", {})
        if not isinstance(persisted, dict) or any(
            not isinstance(label, str) or not isinstance(value, dict)
            for label, value in persisted.items()
        ):
            raise ValueError("invalid EPUB verification report")
        published_outputs.update(persisted)
    if report.get("published") is True:
        label = report.get("output_label")
        if not isinstance(label, str):
            raise ValueError("invalid EPUB verification report")
        published_outputs[label] = report
    return {**report, "published_outputs": published_outputs}


def _manifest_has_invalid_epub_meta(store) -> bool:
    if not os.path.isfile(store.manifest_path):
        return False
    try:
        data = store.read_json(store.manifest_path)
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(data, dict) or data.get("fmt") != "epub" or not data.get("source_path"):
        return False
    meta = data.get("meta")
    return not isinstance(meta, dict) or meta.get("epub_schema") != 4


class RunStore:
    def __init__(self, run_dir: str, *, create: bool = True):
        self.run_dir = run_dir
        self.chapters_dir = os.path.join(run_dir, "chapters")  # V1 遗留（备份）
        self.chapters_v2_dir = os.path.join(run_dir, "chapters_v2")  # V2 活路径
        self.manifest_path = os.path.join(run_dir, "manifest.json")
        self.context_path = os.path.join(run_dir, "context.json")
        self.analysis_path = os.path.join(run_dir, "analysis.json")
        self.glossary_path = os.path.join(run_dir, "glossary.db")
        self.layout_work_path = os.path.join(run_dir, "layout_work.json")
        self.layout_profile_path = os.path.join(run_dir, "layout_profile.json")
        self.report_path = os.path.join(run_dir, "report.json")
        self.epub_verification_path = os.path.join(run_dir, "epub_verification.json")
        self.usage_path = os.path.join(run_dir, "usage.json")
        self.event_log_path = os.path.join(run_dir, "events.jsonl")
        self.journal_path = os.path.join(run_dir, "journal.json")
        self._v2_ready = False
        if create and not _manifest_has_invalid_epub_meta(self):
            self.ensure_dirs()

    def ensure_dirs(self) -> None:
        os.makedirs(self.chapters_v2_dir, exist_ok=True)

    @contextmanager
    def lock(self) -> Iterator[None]:
        if _manifest_has_invalid_epub_meta(self):
            os.makedirs(self.run_dir, exist_ok=True)
        else:
            self.ensure_dirs()
        lock_path = os.path.join(self.run_dir, ".run.lock")
        with open(lock_path, "a+b") as lock_file:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    self._migrate_if_needed()
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    self._migrate_if_needed()
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_migrated(self) -> None:
        if self._v2_ready:
            return
        with self.lock():
            pass

    @staticmethod
    def _validate_epub_manifest(data: object) -> None:
        if not isinstance(data, dict) or data.get("fmt") != "epub" or not data.get("source_path"):
            return
        meta = data.get("meta")
        schema = meta.get("epub_schema") if isinstance(meta, dict) else None
        if schema != 4:
            raise ValueError(
                f"Unsupported EPUB state schema {schema!r}; start a fresh translation for schema 4"
            )

    def _migrate_if_needed(self) -> None:
        """已持锁时调用：迁移、恢复中断节点及检查点日志。"""
        if self._v2_ready:
            _recover_note_migration(self)
            return
        if not os.path.isfile(self.manifest_path):
            _recover_note_migration(self)
            self._v2_ready = True
            return
        data = self.read_json(self.manifest_path)
        self._validate_epub_manifest(data)
        version = data.get("run_state_schema") if isinstance(data, dict) else None
        if version == RUN_STATE_SCHEMA_VERSION:
            state = RunState.model_validate(data)
            raw_progress = data.get("progress") if isinstance(data, dict) else {}
            if isinstance(raw_progress, dict) and any(
                isinstance(value, dict) and "repair_ledger" not in value
                for value in raw_progress.values()
            ):
                self.write_json(self.manifest_path, state.model_dump(mode="json"))
            self._v2_ready = True
        elif version == 3:
            migrate_v3_to_v4(self)
            self._v2_ready = True
        elif version == 2:
            migrate_v2_to_v3(self)
            self._v2_ready = True
        else:
            migrate_v1_to_v2(self)
            self._v2_ready = True
        _recover_note_migration(self)
        state = self.load_state()
        if state.recover_interrupted():
            self.save_state(state)
        import trans_novel.pipeline.state.checkpoint as checkpoint

        checkpoint.recover(self)

    def chapter_path(self, ci: int) -> str:
        return self.chapter_path_v2(ci)

    def chapter_path_v1(self, ci: int) -> str:
        return os.path.join(self.chapters_dir, f"ch{ci}.json")

    def chapter_path_v2(self, ci: int) -> str:
        return os.path.join(self.chapters_v2_dir, f"ch{ci}.json")

    def path_for(self, name: str) -> str:
        return os.path.join(self.run_dir, name)

    def save_journal(self, record: dict | None) -> None:
        if record is None:
            self.clear_journal()
            return
        self.write_json(self.journal_path, record)

    def load_journal(self) -> dict | None:
        if not os.path.isfile(self.journal_path):
            return None
        data = self.read_json(self.journal_path)
        return data if isinstance(data, dict) else None

    def clear_journal(self) -> None:
        if os.path.isfile(self.journal_path):
            os.remove(self.journal_path)

    @staticmethod
    def write_json(path: str, data) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # 原子替换，防写一半中断

    @staticmethod
    def read_json(path: str):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def exists(self) -> bool:
        return os.path.isfile(self.manifest_path)

    def load_state(self) -> RunState:
        self._ensure_migrated()
        raw = self.read_json(self.manifest_path)
        self._validate_epub_manifest(raw)
        return RunState.model_validate(raw)

    def save_state(self, state: RunState) -> None:
        self._ensure_migrated()
        self.write_json(self.manifest_path, state.model_dump(mode="json"))

    def load_manifest(self) -> dict:
        manifest = self.load_state().model_dump(mode="json")
        self._validate_epub_manifest(manifest)
        return manifest

    def save_manifest(self, manifest: dict) -> None:
        self.save_state(RunState.model_validate(manifest))

    def stage_document(self, doc: Document, identity: RunIdentity) -> dict:
        state = RunState(
            run_state_schema=RUN_STATE_SCHEMA_VERSION,
            identity=identity.model_copy(
                update={"translation_policy_version": TRANSLATION_POLICY_VERSION}
            ),
            title=doc.title,
            fmt=doc.fmt,
            source_path=doc.source_path,
            source_lang=doc.source_lang,
            target_lang=doc.target_lang,
            meta=doc.meta,
            chapters=[
                ChapterIndex(
                    index=c.index,
                    title=c.title,
                    href=c.href,
                    toc_entry_id=c.meta.get("toc_entry_id"),
                    processing=c.processing,
                )
                for c in doc.chapters
            ],
            progress={c.index: ChapterProgress() for c in doc.chapters},
        )
        for c in doc.chapters:
            self.save_chapter(c)
        return state.model_dump(mode="json")

    def load_progress(self, ci: int) -> ChapterProgress:
        state = self.load_state()
        return state.progress.get(ci, ChapterProgress())

    def save_progress(self, ci: int, progress: ChapterProgress) -> None:
        state = self.load_state()
        state.progress[ci] = progress
        self.save_state(state)

    def chapter_status(self, ci: int) -> str:
        return self.load_progress(ci).status

    def set_chapter_status(self, ci: int, status: str) -> None:
        state = self.load_state()
        progress = state.progress.setdefault(ci, ChapterProgress())
        progress.status = status  # 仅接受 STATUS_PENDING/STATUS_DONE 常量
        self.save_state(state)

    def pending_chapters(self) -> list[int]:
        state = self.load_state()
        return [ci for ci, pg in state.progress.items() if pg.status != STATUS_DONE]

    # ── 节点状态 / 指纹 ────────────────────────────────────────────────────
    def mark_node_running(self, key: str) -> None:
        state = self.load_state()
        mark_node_running(state, key)
        self.save_state(state)

    def record_node_fingerprint(self, key: str, fingerprint: str) -> None:
        state = self.load_state()
        record_node_fingerprint(state, key, fingerprint)
        self.save_state(state)

    def record_node_output(self, key: str, output: dict) -> None:
        state = self.load_state()
        record_node_output(state, key, output)
        self.save_state(state)

    def mark_node_succeeded(self, key: str, fingerprint: str | None = None) -> None:
        state = self.load_state()
        mark_node_succeeded(state, key, fingerprint)
        self.save_state(state)

    def mark_node_skipped(self, key: str) -> None:
        state = self.load_state()
        mark_node_skipped(state, key)
        self.save_state(state)

    def fail_node(self, key: str, kind: str, message: str = "") -> None:
        state = self.load_state()
        fail_node(state, key, kind, message)
        self.save_state(state)

    def reconcile_fingerprints(self, computed: dict[str, str]) -> set[str]:
        state = self.load_state()
        invalidated = reconcile_fingerprints(state, computed)
        for key in list(invalidated):
            base, separator, suffix = key.partition(":")
            ci = int(suffix) if separator and suffix.isdigit() else None
            if base == "translate" and ci is not None:
                chapter = self.load_chapter(ci)
                clear_translation_targets(chapter, state, ci)
                self.save_chapter(chapter)
                self.log_event(
                    "translate_invalidated", chapter=ci, reason="input_fingerprint_mismatch"
                )
            elif base == "titles":
                clear_translated_titles(state)
                self.log_event("titles_invalidated", reason="input_fingerprint_mismatch")
        if "analyze" in invalidated and os.path.isfile(self.analysis_path):
            os.remove(self.analysis_path)
        self.save_state(state)
        return invalidated

    def verify_identity(
        self,
        *,
        source_bytes_sha256: str,
        source_lang: str = "",
        target_lang: str = "",
    ) -> None:
        state = self.load_state()
        ident = state.identity
        problems: list[str] = []
        if ident.source_bytes_sha256:
            if not source_bytes_sha256:
                problems.append("无法读取当前源文件，不能核验运行身份")
            elif source_bytes_sha256 != ident.source_bytes_sha256:
                problems.append("源文件内容与运行状态不一致（文件被修改或不是同一本书）")
        else:
            problems.append(
                "运行状态缺少源文件校验信息（迁移时源文件不可读），无法确认与当前输入一致"
            )
        if ident.run_input_schema_version != RUN_INPUT_SCHEMA_VERSION:
            problems.append(
                f"运行输入 schema 版本不一致"
                f"（状态 {ident.run_input_schema_version}，当前 {RUN_INPUT_SCHEMA_VERSION}）"
            )
        if (
            source_lang
            and ident.source_lang
            and normalize_lang_code(source_lang) != ident.source_lang
        ):
            problems.append(f"源语言不一致（状态 {ident.source_lang}，当前 {source_lang}）")
        if (
            target_lang
            and ident.target_lang
            and normalize_lang_code(target_lang) != ident.target_lang
        ):
            problems.append(f"目标语言不一致（状态 {ident.target_lang}，当前 {target_lang}）")
        if problems:
            raise IdentityMismatchError("；".join(problems))

    def save_chapter(self, chapter: Chapter) -> None:
        self._ensure_migrated()
        self.write_json(self.chapter_path(chapter.index), chapter.to_dict())

    def load_chapter(self, ci: int) -> Chapter:
        self._ensure_migrated()
        return Chapter.from_dict(self.read_json(self.chapter_path(ci)))

    def save_context(self, data: dict) -> None:
        self.write_json(self.context_path, data)

    def load_context(self) -> dict | None:
        return self.read_json(self.context_path) if os.path.isfile(self.context_path) else None

    def save_analysis(self, data: dict) -> None:
        self._ensure_migrated()
        self.write_json(self.analysis_path, data)

    def load_analysis(self) -> dict | None:
        self._ensure_migrated()
        return self.read_json(self.analysis_path) if os.path.isfile(self.analysis_path) else None

    def load_layout_work(self) -> dict | None:
        return (
            self.read_json(self.layout_work_path) if os.path.isfile(self.layout_work_path) else None
        )

    def load_layout_profile(self) -> dict | None:
        return (
            self.read_json(self.layout_profile_path)
            if os.path.isfile(self.layout_profile_path)
            else None
        )

    def save_report(self, data: dict) -> None:
        self.write_json(self.report_path, data)

    def save_epub_verification(self, data: dict) -> None:
        merged = _merge_epub_verification(self, data)
        data["published_outputs"] = merged["published_outputs"]
        self.write_json(self.epub_verification_path, merged)

    def load_epub_verification(self) -> dict | None:
        if not os.path.isfile(self.epub_verification_path):
            return None
        value = self.read_json(self.epub_verification_path)
        return value if isinstance(value, dict) else None

    def load_usage(self) -> dict | None:
        return self.read_json(self.usage_path) if os.path.isfile(self.usage_path) else None

    def _event_row(self, event: str, **data: Any) -> dict[str, Any]:
        return {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "event_schema": 2,
            **data,
        }

    def log_event_required(self, event: str, **data: Any) -> None:
        row = self._event_row(event, **data)
        self.ensure_dirs()
        with open(self.event_log_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def log_event(self, event: str, **data: Any) -> None:
        row = self._event_row(event, **data)
        try:
            self.ensure_dirs()
            with open(self.event_log_path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as exc:
            warnings.warn(
                f"event log append failed for {event!r}: {exc}", RuntimeWarning, stacklevel=2
            )
