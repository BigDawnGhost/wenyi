"""Review 服务：只读影子译文的并行审校、证据 Agent Loop、冲突仲裁、Fixer 与盲复审状态机。

Review 每次创建新的独立目录，只修改本次运行的 shadow overlay；正式 chapter、
manifest 和 glossary 保持只读。保留按输入位置恢复结果顺序、在线程外排序写入恢复
事件等确定性保证。Review 顶层异常仍须先保存 failed/partial result、诊断记录和本次
usage，再原样抛出；单个 Fixer 失败继续转成 unresolved 项。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from ..agents.review_fixer import (
    ProvisionalPatch,
    ReviewFixer,
    ReviewFixerProtocolError,
)
from ..agents.review_loop import (
    ReviewAgentLoop,
    ReviewConflictArbiter,
    apply_review_arbitrations,
    build_conflict_groups,
    normalize_review_issues,
)
from ..agents.reviewer import ReviewOutputError
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..llm.usage import usage_delta
from ..review.evidence import BookEvidenceIndex
from ..review.run_store import ReviewOutcome, ReviewRunStore
from .runstore import STATUS_DONE

if TYPE_CHECKING:
    from .runstore import RunStore
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


@dataclass(frozen=True)
class _ReviewRoundResult:
    """一次全书影子译文 Review 及冲突仲裁后的确定性结果。"""

    issues: list[dict[str, Any]]
    pre_arbitration_issues: list[dict[str, Any]]
    arbitration_superseded: list[dict[str, Any]]
    conflict_groups: list[dict[str, Any]]
    residual_conflicts: list[dict[str, Any]]
    fallback_agent_count: int


def _review_overlay_digest(
    chapters,
    overrides: Mapping[tuple[int, int], str],
) -> str:
    """计算全书有效影子译文指纹，用于检测无进展与 A↔B 振荡。"""
    payload = [
        (
            chapter.index,
            text_index,
            overrides.get((chapter.index, text_index), segment.target or ""),
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_content_digest(chapters) -> str:
    """计算本次 Review 实际读取的正式正文摘要。"""
    payload = [
        (
            chapter.index,
            text_index,
            segment.index,
            segment.anchor or "",
            segment.kind,
            segment.source,
            segment.target or "",
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_net_changes(
    chapters,
    overrides: Mapping[tuple[int, int], str],
    patch_records: list[dict[str, Any]],
    active_patches: Mapping[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """把多轮影子补丁折叠成每段一条的最终修改建议。"""
    baseline = {
        (chapter.index, text_index): segment.target or ""
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    }
    issue_keys_by_location: dict[tuple[int, int], set[str]] = {}
    for patch in patch_records:
        chapter = patch.get("chapter")
        index = patch.get("index")
        if (
            not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or patch.get("status") == "rejected_cycle"
        ):
            continue
        keys = issue_keys_by_location.setdefault((chapter, index), set())
        keys.update(str(key) for key in patch.get("issue_keys", []) if isinstance(key, str) and key)

    changes: list[dict[str, Any]] = []
    for location, suggested_target in sorted(overrides.items()):
        if baseline.get(location) == suggested_target:
            continue
        active = active_patches.get(location) or {}
        changes.append(
            {
                "chapter": location[0],
                "index": location[1],
                "suggested_target": suggested_target,
                "issue_keys": sorted(issue_keys_by_location.get(location, set())),
                "review_result": str(active.get("status") or "provisional"),
            }
        )
    return changes


def _review_public_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """裁剪内部审校字段，生成面向用户的稳定问题列表。"""
    public: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_key = issue.get("issue_key")
        chapter = issue.get("chapter")
        index = issue.get("index")
        if (
            not isinstance(issue_key, str)
            or not issue_key
            or not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            continue
        public[issue_key] = {
            "issue_key": issue_key,
            "chapter": chapter,
            "index": index,
            "type": str(issue.get("type") or ""),
            "detail": str(issue.get("detail") or ""),
            "suggestion": str(issue.get("suggestion") or ""),
        }
    return sorted(
        public.values(),
        key=lambda issue: (issue["chapter"], issue["index"], issue["issue_key"]),
    )


def _review_conflict_records(
    groups: list[dict[str, Any]],
    arbitrations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把冲突组及对应仲裁结果序列化为稳定的逐轮记录。"""
    return [
        {
            "conflict_id": group["conflict_id"],
            "consistency_key": group["consistency_key"],
            "issue_ids": [issue["issue_id"] for issue in group["issues"]],
            "proposals": [
                {
                    "issue_id": issue["issue_id"],
                    "chapter": issue["chapter"],
                    "index": issue["index"],
                    "proposed_value": issue["consistency"]["proposed_value"],
                }
                for issue in group["issues"]
            ],
            "arbitration": arbitration,
        }
        for group, arbitration in zip(groups, arbitrations)
    ]


def _review_unresolved_conflict_records(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从最终未解决问题重建冲突记录，避免被最后一轮空结果掩盖。"""
    groups = build_conflict_groups(issues)
    arbitrations: list[dict[str, Any]] = []
    for group in groups:
        issue_ids = [str(issue["issue_id"]) for issue in group["issues"]]
        annotations = [
            issue.get("arbitration")
            for issue in group["issues"]
            if isinstance(issue.get("arbitration"), dict)
        ]
        reasons = [
            str(annotation.get("reason", "")).strip()
            for annotation in annotations
            if str(annotation.get("reason", "")).strip()
        ]
        evidence_refs = sorted(
            {
                str(ref)
                for issue in group["issues"]
                for ref in issue.get("evidence_refs", [])
                if isinstance(ref, str) and ref
            }
        )
        arbitrations.append(
            {
                "conflict_id": group["conflict_id"],
                "consistency_key": group["consistency_key"],
                "issue_ids": issue_ids,
                "status": "unresolved",
                "recommended_value": "",
                "reason": reasons[-1] if reasons else "最终未解决问题仍包含互斥建议。",
                "supported_issue_ids": issue_ids,
                "rejected_issue_ids": [],
                "evidence_refs": evidence_refs,
            }
        )
    return _review_conflict_records(groups, arbitrations)


def _review_unresolved_fallback_count(issues: list[dict[str, Any]]) -> int:
    """统计最终未解决问题中仍由降级 Agent 产生的独立审校块。"""
    return len(
        {
            str(issue.get("_chunk_id") or issue.get("issue_key") or issue.get("issue_id"))
            for issue in issues
            if issue.get("agent_fallback")
        }
    )


class ReviewService:
    """只读全书 Agent Review 的领域服务。"""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    def session_terms(
        self,
        store: RunStore,
        glossary: GlossaryStore | None = None,
    ) -> list[GlossaryTerm]:
        """返回本次 Review 使用的最终术语库快照。"""
        if glossary is not None:
            return glossary.all_terms()
        return GlossaryStore.load_terms_readonly(store.glossary_path)

    def _review_config_snapshot(self) -> dict[str, Any]:
        """当前审校相关配置快照，供 metadata 落盘与 skip 判定对比。"""
        return {
            "review_concurrency": self._runtime.config.pipeline.review_concurrency,
            "review_output_retries": self._runtime.config.pipeline.review_output_retries,
            "review_agent_loop": self._runtime.config.pipeline.review_agent_loop,
            "review_agent_tier": self._runtime.config.pipeline.review_agent_tier,
            "review_agent_max_evidence_rounds": (
                self._runtime.config.pipeline.review_agent_max_evidence_rounds
            ),
            "review_conflict_arbitration": (
                self._runtime.config.pipeline.review_conflict_arbitration
            ),
            "review_fix_loop": self._runtime.config.pipeline.review_fix_loop,
            "review_fix_max_rounds": self._runtime.config.pipeline.review_fix_max_rounds,
            "review_clean_confirmations": (
                self._runtime.config.pipeline.review_clean_confirmations
            ),
        }

    @staticmethod
    def _review_glossary_fingerprint(terms: list[GlossaryTerm]) -> str:
        """术语表内容指纹：术语表变化后已完成的 Review 结果不得复用。"""
        ordered = sorted((term.source, term.target, term.type) for term in terms)
        return hashlib.sha256(json.dumps(ordered, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _review_skip_eligible(
        self,
        store: RunStore,
        latest: dict[str, Any],
        terms: list[GlossaryTerm],
    ) -> bool:
        """已完成 Review 结果能否安全复用：内容、配置与术语表必须全部一致。"""
        review_id = latest.get("review_id")
        if not isinstance(review_id, str) or not review_id:
            return False
        metadata_path = os.path.join(store.run_dir, "reviews", review_id, "rounds", "metadata.json")
        try:
            with open(metadata_path, encoding="utf-8") as f:
                metadata = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        saved_config = metadata.get("config")
        saved_glossary = metadata.get("glossary_fingerprint")
        return (
            isinstance(saved_config, dict)
            and saved_config == self._review_config_snapshot()
            and isinstance(saved_glossary, str)
            and saved_glossary == self._review_glossary_fingerprint(terms)
        )

    @staticmethod
    def _review_usage_from_dir(store: RunStore, review_id: str) -> dict[str, Any]:
        """读取已完成的 Review 目录用量；缺失时返回空（调用方容忍）。"""
        try:
            with open(
                os.path.join(store.run_dir, "reviews", review_id, "usage.json"),
                encoding="utf-8",
            ) as f:
                usage = json.load(f)
            return usage if isinstance(usage, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def review_round(
        self,
        loaded,
        all_terms: list[GlossaryTerm],
        evidence: BookEvidenceIndex,
        debug: ReviewRunStore,
        *,
        review_round: int,
        target_overrides: Mapping[tuple[int, int], str],
        progress: ProgressFn | None = None,
    ) -> _ReviewRoundResult:
        """对同一份只读影子译文完成一轮全书审校和冲突仲裁。"""
        total = sum(len(chapter.text_segments) for chapter in loaded)
        done = 0
        review_label = (
            f"全书审校 R{review_round}" if review_round == 1 else f"全书盲审 R{review_round}"
        )
        if progress:
            progress(0, total, review_label)
        raw_issues: list[dict[str, Any]] = []
        for chapter in loaded:
            text_segs = chapter.text_segments
            if self._runtime.config.pipeline.glossary_scope == "chapter":
                source_text = "\n".join(segment.source for segment in text_segs)
                term_snapshot = GlossaryStore.terms_in(all_terms, source_text)
            else:
                term_snapshot = all_terms

            def on_chunk_finished(segment_count: int) -> None:
                """在一个顶层审校块完成后推进本轮全书段落进度。"""
                nonlocal done
                done += segment_count
                if progress:
                    progress(done, total, review_label)

            chapter_issues = self.review_chapter(
                text_segs,
                term_snapshot,
                chapter_index=chapter.index,
                evidence=evidence,
                debug=debug,
                target_overrides=target_overrides,
                review_round=review_round,
                on_chunk_finished=on_chunk_finished,
            )
            for issue in chapter_issues:
                issue["chapter"] = chapter.index
                issue["stage"] = "review_agent"
                issue["review_round"] = review_round
            raw_issues.extend(chapter_issues)
            debug.log_event(
                "review_chapter_finished",
                chapter=chapter.index,
                segment_count=len(text_segs),
                issue_count=len(chapter_issues),
            )

        pre_arbitration_issues = normalize_review_issues(raw_issues, evidence)
        for issue in pre_arbitration_issues:
            issue["issue_id"] = f"r{review_round}-{issue['issue_id']}"
        conflict_groups = build_conflict_groups(pre_arbitration_issues)
        arbitrations: list[dict[str, Any]] = []
        if conflict_groups and self._runtime.config.pipeline.review_conflict_arbitration:
            arbitration_label = f"冲突仲裁 R{review_round}"
            arbitration_total = len(conflict_groups)
            if progress:
                progress(0, arbitration_total, arbitration_label)
            workers = min(
                max(1, self._runtime.config.pipeline.review_concurrency),
                arbitration_total,
            )

            def arbitrate(group: dict[str, Any]) -> dict[str, Any]:
                return ReviewConflictArbiter(
                    self._runtime.client,
                    self._runtime.config,
                    evidence,
                    debug,
                ).arbitrate(group)

            if workers == 1:
                for done_count, group in enumerate(conflict_groups, start=1):
                    arbitrations.append(arbitrate(group))
                    if progress:
                        progress(done_count, arbitration_total, arbitration_label)
            else:
                ordered_arbitrations: list[dict[str, Any] | None] = [None] * arbitration_total
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(arbitrate, group): position
                        for position, group in enumerate(conflict_groups)
                    }
                    for done_count, future in enumerate(as_completed(futures), start=1):
                        ordered_arbitrations[futures[future]] = future.result()
                        if progress:
                            progress(done_count, arbitration_total, arbitration_label)
                arbitrations = [
                    arbitration for arbitration in ordered_arbitrations if arbitration is not None
                ]
        elif conflict_groups:
            arbitrations = [
                {
                    "conflict_id": group["conflict_id"],
                    "consistency_key": group["consistency_key"],
                    "issue_ids": [issue["issue_id"] for issue in group["issues"]],
                    "status": "unresolved",
                    "recommended_value": "",
                    "reason": "配置已关闭全书冲突仲裁。",
                    "supported_issue_ids": [issue["issue_id"] for issue in group["issues"]],
                    "rejected_issue_ids": [],
                    "evidence_refs": [],
                }
                for group in conflict_groups
            ]

        final_issues, arbitration_superseded = apply_review_arbitrations(
            pre_arbitration_issues,
            arbitrations,
        )
        fallback_agent_count = len(
            {
                issue["_chunk_id"]
                for issue in pre_arbitration_issues
                if issue.get("agent_fallback") and isinstance(issue.get("_chunk_id"), str)
            }
        )
        residual_conflicts = build_conflict_groups(final_issues)
        initial_issues, dismissed = debug.result_snapshots(review_round)
        debug.write_json("initial_issues.json", initial_issues)
        debug.write_json("dismissed_issues.json", dismissed)
        debug.write_json("pre_arbitration_issues.json", pre_arbitration_issues)
        debug.write_json("arbitration_superseded_issues.json", arbitration_superseded)
        debug.write_json("final_issues.json", final_issues)
        debug.write_json(
            "residual_conflicts.json",
            [
                {
                    "conflict_id": group["conflict_id"],
                    "consistency_key": group["consistency_key"],
                    "issue_ids": [issue["issue_id"] for issue in group["issues"]],
                }
                for group in residual_conflicts
            ],
        )
        debug.write_json(
            "conflicts.json",
            _review_conflict_records(conflict_groups, arbitrations),
        )
        debug.log_event(
            "review_round_finished",
            issue_count=len(final_issues),
            conflict_count=len(conflict_groups),
            unresolved_conflict_count=len(residual_conflicts),
            fallback_agent_count=fallback_agent_count,
        )
        return _ReviewRoundResult(
            issues=final_issues,
            pre_arbitration_issues=pre_arbitration_issues,
            arbitration_superseded=arbitration_superseded,
            conflict_groups=conflict_groups,
            residual_conflicts=residual_conflicts,
            fallback_agent_count=fallback_agent_count,
        )

    def propose_review_patches(
        self,
        round_result: _ReviewRoundResult,
        evidence: BookEvidenceIndex,
        all_terms: list[GlossaryTerm],
        analysis: dict[str, Any],
        debug: ReviewRunStore,
        *,
        review_round: int,
        fix_round: int,
        progress: ProgressFn | None = None,
    ) -> tuple[list[ProvisionalPatch], list[dict[str, Any]]]:
        """按段聚合已确认问题，并行生成仅供下一轮验证的完整段落替换。"""
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        skipped: list[dict[str, Any]] = []
        for issue in round_result.issues:
            chapter = issue.get("chapter")
            index = issue.get("index")
            issue_id = issue.get("issue_id")
            if (
                isinstance(chapter, bool)
                or not isinstance(chapter, int)
                or isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(issue_id, str)
            ):
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "status": "skipped",
                        "reason": "invalid_issue_location",
                    }
                )
                continue
            arbitration = issue.get("arbitration")
            if isinstance(arbitration, dict) and arbitration.get("status") == "unresolved":
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "chapter": chapter,
                        "index": index,
                        "status": "skipped",
                        "reason": "unresolved_consistency_conflict",
                    }
                )
                continue
            if self._runtime.config.pipeline.review_agent_loop and issue.get("agent_fallback"):
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "chapter": chapter,
                        "index": index,
                        "status": "skipped",
                        "reason": "unverified_agent_fallback",
                    }
                )
                continue
            grouped.setdefault((chapter, index), []).append(issue)

        jobs = sorted(grouped.items())
        if not jobs:
            return [], skipped
        fix_label = f"影子修订 R{fix_round}"
        fix_total = len(jobs)
        if progress:
            progress(0, fix_total, fix_label)
        style = self._runtime.analyzer.style_brief(analysis)
        book_synopsis = str(analysis.get("book_synopsis", "") or "")
        fixer = ReviewFixer(self._runtime.client, self._runtime.config)

        def propose(
            job: tuple[tuple[int, int], list[dict[str, Any]]],
        ) -> tuple[ProvisionalPatch | None, dict[str, Any] | None]:
            (chapter, index), issues = job
            segment = evidence.segment_ref(chapter, index)
            if segment is None:
                return None, {
                    "issue_ids": [issue["issue_id"] for issue in issues],
                    "chapter": chapter,
                    "index": index,
                    "status": "skipped",
                    "reason": "segment_not_found",
                }
            context = evidence.segment_context(
                {
                    "chapter": chapter,
                    "index": index,
                    "before": 4,
                    "after": 4,
                }
            )
            context_segments = context.get("segments", []) if context.get("ok") else []
            nearby_pairs = [
                (str(item.get("source", "")), str(item.get("target", "")))
                for item in context_segments
                if isinstance(item, dict) and item.get("ref") != segment.ref
            ]
            context_source = "\n".join(
                str(item.get("source", "")) for item in context_segments if isinstance(item, dict)
            )
            relevant_terms = GlossaryStore.terms_in(
                all_terms,
                context_source or segment.source,
            )
            trace_path = f"fixers/ch{chapter}-text{index}.json"
            trace: dict[str, Any] = {
                "chapter": chapter,
                "index": index,
                "segment_ref": segment.ref,
                "issue_ids": [issue["issue_id"] for issue in issues],
                "status": "running",
            }
            debug.write_json(trace_path, trace)

            def record(event: str, data: dict[str, Any]) -> None:
                trace[event] = data
                debug.write_json(trace_path, trace)

            try:
                patch = fixer.propose(
                    review_round,
                    segment.ref,
                    chapter,
                    index,
                    segment.source,
                    segment.target,
                    issues,
                    style=style,
                    book_synopsis=book_synopsis,
                    chapter_digest=evidence.chapter_digests.get(chapter, ""),
                    relevant_glossary=relevant_terms,
                    nearby_pairs=nearby_pairs,
                    trace=record,
                )
            except Exception as error:  # noqa: BLE001 - 单段 Fix 失败保留为未解决建议
                trace["status"] = "failed"
                trace["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                debug.write_json(trace_path, trace)
                return None, {
                    "issue_ids": [issue["issue_id"] for issue in issues],
                    "chapter": chapter,
                    "index": index,
                    "segment_ref": segment.ref,
                    "status": "failed",
                    "reason": (
                        str(error)
                        if isinstance(error, ReviewFixerProtocolError)
                        else f"{type(error).__name__}: {error}"
                    ),
                }
            trace["status"] = "finished"
            trace["patch"] = patch.as_dict()
            debug.write_json(trace_path, trace)
            return patch, None

        workers = min(
            max(1, self._runtime.config.pipeline.review_concurrency),
            fix_total,
        )
        if workers == 1:
            results = []
            for done_count, job in enumerate(jobs, start=1):
                results.append(propose(job))
                if progress:
                    progress(done_count, fix_total, fix_label)
        else:
            ordered_results: list[tuple[ProvisionalPatch | None, dict[str, Any] | None] | None] = [
                None
            ] * fix_total
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(propose, job): position for position, job in enumerate(jobs)
                }
                for done_count, future in enumerate(as_completed(futures), start=1):
                    ordered_results[futures[future]] = future.result()
                    if progress:
                        progress(done_count, fix_total, fix_label)
            results = [result for result in ordered_results if result is not None]
        patches = [patch for patch, _ in results if patch is not None]
        failures = [failure for _, failure in results if failure is not None]
        debug.log_event(
            "review_fix_round_finished",
            patch_count=len(patches),
            skipped_count=len(skipped),
            failed_count=len(failures),
        )
        return patches, [*skipped, *failures]

    def run_session(
        self,
        store: RunStore,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """在只读影子译文上循环 Review→临时 Fix→盲复审。

        正式 chapter、manifest 和术语库始终不变。每轮 Fix 只更新内存 overlay
        与本次 Review 目录；下一轮全书 Review 不接收旧问题说明，只读取修改后
        的影子译文。会话摘要、用量与正式事件在结束时持久化。
        """
        manifest = store.load_manifest()
        pending = [
            chapter["index"]
            for chapter in manifest.get("chapters", [])
            if chapter.get("status") != STATUS_DONE
        ]
        if pending:
            joined = ", ".join(str(index) for index in pending[:10])
            suffix = "…" if len(pending) > 10 else ""
            raise ValueError(f"全书审校要求所有章节先完成翻译；仍待翻译章节：{joined}{suffix}")

        chapter_rows = manifest.get("chapters", [])
        loaded = [store.load_chapter(item["index"]) for item in chapter_rows]
        total = sum(len(chapter.text_segments) for chapter in loaded)
        analysis = store.load_analysis() or {}
        reviewed_content_digest = _review_content_digest(loaded)

        # 跳过已完成 review：内容、配置与术语表指纹一致则复用结果
        latest_completed = store.load_latest_review_result()
        if (
            latest_completed is not None
            and latest_completed.get("status") == "completed"
            and latest_completed.get("reviewed_content_digest") == reviewed_content_digest
            and self._review_skip_eligible(store, latest_completed, all_terms)
        ):
            store.log_event("review_skipped", reason="already_completed")
            return ReviewOutcome(
                run_dir=os.path.join(
                    store.run_dir, "reviews", latest_completed.get("review_id", "")
                ),
                result=latest_completed,
                usage=self._review_usage_from_dir(store, latest_completed.get("review_id", "")),
            )

        # 断点续跑：未完成 Review 须内容、审校配置、术语表指纹一致
        debug = ReviewRunStore.find_resumable(
            store.run_dir,
            reviewed_content_digest,
            config=self._review_config_snapshot(),
            glossary_fingerprint=self._review_glossary_fingerprint(all_terms),
        )
        if debug is not None:
            debug.log_event("review_resumed_from_checkpoint", review_id=debug.review_id)
        else:
            debug = ReviewRunStore(store.run_dir)
        debug.start(
            reviewed_content_digest=reviewed_content_digest,
            metadata={
                "source_sha256": manifest.get("source_sha256"),
                "title": manifest.get("title"),
                "source_lang": self._runtime.config.source_lang,
                "target_lang": self._runtime.config.target_lang,
                "chapter_count": len(loaded),
                "total_segments": total,
                "config": self._review_config_snapshot(),
                "glossary_fingerprint": self._review_glossary_fingerprint(all_terms),
            },
        )
        store.log_event(
            "review_started",
            review_id=debug.review_id,
            review_dir=debug.run_dir,
            reviewed_content_digest=reviewed_content_digest,
        )
        usage_before = self._runtime.client.usage_summary()

        def save_review_usage() -> dict[str, Any]:
            """保存本次 Review 增量并合并到本书累计用量。"""
            usage = usage_delta(self._runtime.client.usage_summary(), usage_before)
            debug.save_usage(usage)
            self._runtime.flush_usage(store, scope="review")
            return usage

        target_overrides: dict[tuple[int, int], str] = {}
        seen_overlays = {_review_overlay_digest(loaded, target_overrides)}
        patch_records: list[dict[str, Any]] = []
        active_patches: dict[tuple[int, int], dict[str, Any]] = {}
        fix_failures: list[dict[str, Any]] = []
        blocked_issues: dict[str, dict[str, Any]] = {}
        round_summaries: list[dict[str, Any]] = []
        latest: _ReviewRoundResult | None = None
        clean_streak = 0
        fix_rounds = 0
        termination = "not_started"
        fix_loop = self._runtime.config.pipeline.review_fix_loop
        required_clean = self._runtime.config.pipeline.review_clean_confirmations if fix_loop else 1
        max_review_rounds = (
            (self._runtime.config.pipeline.review_fix_max_rounds + 1) * required_clean
            if fix_loop
            else 1
        )

        # 断点续跑：加载轮级检查点
        _checkpoint = debug.load_checkpoint()
        _resume_scan_done = False
        _resume_latest: _ReviewRoundResult | None = None
        if _checkpoint is not None:
            start_round = _checkpoint.get("next_round", 1)
            # 配置收紧（如降低 review_clean_confirmations）后轮次上限可能变小；
            # 若检查点轮次已超出上限，直接收敛到最后一轮，避免空循环 + RuntimeError。
            start_round = min(start_round, max_review_rounds)
            target_overrides = {
                (o["chapter"], o["index"]): o["target"]
                for o in _checkpoint.get("target_overrides", [])
            }
            seen_overlays = set(_checkpoint.get("seen_overlays", []))
            patch_records = _checkpoint.get("patch_records", [])
            active_patches = {
                (p["chapter"], p["index"]): p for p in _checkpoint.get("active_patches", [])
            }
            fix_failures = _checkpoint.get("fix_failures", [])
            blocked_issues = _checkpoint.get("blocked_issues", {})
            round_summaries = _checkpoint.get("round_summaries", [])
            clean_streak = _checkpoint.get("clean_streak", 0)
            fix_rounds = _checkpoint.get("fix_rounds", 0)
            # 恢复 round 内部相位：scan_done 表示扫描已完成，可跳过
            if _checkpoint.get("phase") == "scan_done":
                _resume_scan_done = True
                start_round = _checkpoint.get("next_round", 1)
                # 配置收紧导致上限小于检查点轮次时，不能复用旧轮的扫描结果
                if start_round > max_review_rounds:
                    _resume_scan_done = False
                    _resume_latest = None
                    start_round = max_review_rounds
                else:
                    _resume_latest = _ReviewRoundResult(
                        issues=_checkpoint.get("latest_issues", []),
                        pre_arbitration_issues=_checkpoint.get("latest_pre_arbitration_issues", []),
                        arbitration_superseded=_checkpoint.get("latest_arbitration_superseded", []),
                        conflict_groups=_checkpoint.get("latest_conflict_groups", []),
                        residual_conflicts=_checkpoint.get("latest_residual_conflicts", []),
                        fallback_agent_count=_checkpoint.get("latest_fallback_agent_count", 0),
                    )
                debug.log_event(
                    "review_checkpoint_restored",
                    next_round=start_round,
                    phase="scan_done",
                    override_count=len(target_overrides),
                    fix_rounds=fix_rounds,
                    clean_streak=clean_streak,
                )
            else:
                debug.log_event(
                    "review_checkpoint_restored",
                    next_round=start_round,
                    phase="round_done",
                    override_count=len(target_overrides),
                    fix_rounds=fix_rounds,
                    clean_streak=clean_streak,
                )
        else:
            start_round = 1

        def _save_checkpoint(
            current_round: int,
            phase: str = "round_done",
            latest: _ReviewRoundResult | None = None,
        ) -> None:
            """保存轮级检查点。phase: round_done | scan_done"""
            state: dict[str, Any] = {
                "phase": phase,
                "next_round": current_round + 1 if phase == "round_done" else current_round,
                "target_overrides": [
                    {"chapter": c, "index": i, "target": t}
                    for (c, i), t in sorted(target_overrides.items())
                ],
                "seen_overlays": sorted(seen_overlays),
                "patch_records": patch_records,
                "active_patches": [
                    {**p, "chapter": c, "index": i} for (c, i), p in sorted(active_patches.items())
                ],
                "fix_failures": fix_failures,
                "blocked_issues": blocked_issues,
                "round_summaries": round_summaries,
                "clean_streak": clean_streak,
                "fix_rounds": fix_rounds,
            }
            if latest is not None:
                state["latest_issues"] = latest.issues
                state["latest_pre_arbitration_issues"] = latest.pre_arbitration_issues
                state["latest_arbitration_superseded"] = latest.arbitration_superseded
                state["latest_conflict_groups"] = latest.conflict_groups
                state["latest_residual_conflicts"] = latest.residual_conflicts
                state["latest_fallback_agent_count"] = latest.fallback_agent_count
            debug.save_checkpoint(state)

        def register_blocked(
            issues: list[dict[str, Any]],
            failures: list[dict[str, Any]],
        ) -> None:
            """按稳定问题键保留 Fix 失败项，避免后续 Reviewer 漏报后假 clean。"""
            by_id = {
                str(issue["issue_id"]): issue
                for issue in issues
                if isinstance(issue.get("issue_id"), str)
            }
            for failure in failures:
                failure_ids = failure.get("issue_ids")
                if not isinstance(failure_ids, list):
                    failure_id = failure.get("issue_id")
                    failure_ids = [failure_id] if isinstance(failure_id, str) else []
                for issue_id in failure_ids:
                    issue = by_id.get(str(issue_id))
                    if issue is None:
                        continue
                    issue_key = issue.get("issue_key")
                    if not isinstance(issue_key, str) or not issue_key:
                        continue
                    blocked_issues[issue_key] = {
                        **dict(issue),
                        "fix_failure": {
                            "status": failure.get("status"),
                            "reason": failure.get("reason"),
                            "review_round": failure.get("review_round"),
                        },
                    }

        def effective_issues(current: _ReviewRoundResult) -> list[dict[str, Any]]:
            """合并本轮问题与历史未修项，按书序返回公开的未解决问题。"""
            combined = {
                str(issue["issue_key"]): dict(issue)
                for issue in current.issues
                if isinstance(issue.get("issue_key"), str)
            }
            for issue_key, blocked in blocked_issues.items():
                current_issue = combined.get(issue_key)
                if current_issue is None:
                    combined[issue_key] = dict(blocked)
                    continue
                fix_failure = blocked.get("fix_failure")
                if isinstance(fix_failure, dict):
                    current_issue["fix_failure"] = dict(fix_failure)
            return sorted(
                combined.values(),
                key=lambda issue: (
                    issue.get("chapter", -1),
                    issue.get("index", -1),
                    issue.get("review_round", -1),
                    issue.get("issue_id", ""),
                ),
            )

        try:
            for review_round in range(start_round, max_review_rounds + 1):
                overlay_digest = _review_overlay_digest(loaded, target_overrides)
                evidence = BookEvidenceIndex(
                    loaded,
                    all_terms,
                    analysis,
                    target_overrides=target_overrides,
                )
                with debug.round_scope(review_round):
                    debug.log_event(
                        "review_round_started",
                        overlay_digest=overlay_digest,
                        override_count=len(target_overrides),
                    )
                    debug.write_json(
                        "overlay.json",
                        [
                            {
                                "chapter": chapter,
                                "index": index,
                                "target": target,
                            }
                            for (chapter, index), target in sorted(target_overrides.items())
                        ],
                    )
                    # 断点续跑：scan_done 恢复时跳过扫描，直接用缓存结果
                    if (
                        _resume_scan_done
                        and review_round == start_round
                        and _resume_latest is not None
                    ):
                        latest = _resume_latest
                        _resume_scan_done = False
                        _resume_latest = None
                        # 跳过扫描后重建初审/驳回快照，保证最终报告数据完整
                        debug.rebuild_snapshots_from_chunks(review_round)
                        debug.log_event("review_scan_skipped", review_round=review_round)
                    else:
                        latest = self.review_round(
                            loaded,
                            all_terms,
                            evidence,
                            debug,
                            review_round=review_round,
                            target_overrides=target_overrides,
                            progress=progress,
                        )
                        # 断点续跑：扫描完成，保存 mid-round 检查点
                        _save_checkpoint(review_round, phase="scan_done", latest=latest)
                        # 提前落盘本次扫描用量，避免 fix 阶段崩溃导致用量丢失
                        save_review_usage()
                        usage_before = self._runtime.client.usage_summary()

                    current_issue_keys = {
                        str(issue["issue_key"])
                        for issue in latest.issues
                        if isinstance(issue.get("issue_key"), str)
                    }
                    for patch_record in active_patches.values():
                        if patch_record.get("round", review_round) >= review_round:
                            continue
                        covered_issue_keys = {
                            str(issue_key)
                            for issue_key in patch_record.get("issue_keys", [])
                            if isinstance(issue_key, str)
                        }
                        rereported = sorted(covered_issue_keys & current_issue_keys)
                        not_rereported = sorted(covered_issue_keys - current_issue_keys)
                        for issue_key in not_rereported:
                            blocked_issues.pop(issue_key, None)
                        patch_record["rereported_issue_keys"] = rereported
                        patch_record["not_rereported_issue_keys"] = not_rereported
                        if rereported:
                            patch_record["status"] = "needs_revision"
                            patch_record["failed_review_round"] = review_round
                        else:
                            if patch_record.get("status") != "not_rereported":
                                patch_record["not_rereported_in_round"] = review_round
                            patch_record["status"] = "not_rereported"

                    round_summary: dict[str, Any] = {
                        "review_round": review_round,
                        "overlay_digest": overlay_digest,
                        "override_count": len(target_overrides),
                        "issue_count": len(latest.issues),
                        "conflict_count": len(latest.conflict_groups),
                        "unresolved_conflict_count": len(latest.residual_conflicts),
                        "fallback_agent_count": latest.fallback_agent_count,
                        "clean_streak_before": clean_streak,
                        "blocked_issue_count": len(blocked_issues),
                    }
                    if not latest.issues:
                        if blocked_issues:
                            clean_streak = 0
                            if progress:
                                progress(0, required_clean, "干净确认")
                            termination = "unresolved_fixes"
                            round_summary["clean_streak_after"] = 0
                            round_summary["patch_count"] = 0
                            round_summary["termination"] = termination
                            debug.write_json("summary.json", round_summary)
                            round_summaries.append(round_summary)
                            _save_checkpoint(review_round)
                            break
                        clean_streak += 1
                        if progress:
                            progress(clean_streak, required_clean, "干净确认")
                        round_summary["clean_streak_after"] = clean_streak
                        round_summary["patch_count"] = 0
                        if clean_streak >= required_clean:
                            termination = "clean_confirmed"
                            round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        if termination == "clean_confirmed":
                            _save_checkpoint(review_round)
                            break
                        _save_checkpoint(review_round)
                        continue

                    if clean_streak and progress:
                        progress(0, required_clean, "干净确认")
                    clean_streak = 0
                    round_summary["clean_streak_after"] = 0
                    if not fix_loop:
                        termination = "issues_reported"
                        round_summary["patch_count"] = 0
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break
                    if fix_rounds >= self._runtime.config.pipeline.review_fix_max_rounds:
                        termination = "max_rounds"
                        round_summary["patch_count"] = 0
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break

                    patches, failures = self.propose_review_patches(
                        latest,
                        evidence,
                        all_terms,
                        analysis,
                        debug,
                        review_round=review_round,
                        fix_round=fix_rounds + 1,
                        progress=progress,
                    )
                    fix_failures.extend(
                        [
                            {
                                **failure,
                                "review_round": review_round,
                            }
                            for failure in failures
                        ]
                    )
                    register_blocked(
                        latest.issues,
                        [
                            {
                                **failure,
                                "review_round": review_round,
                            }
                            for failure in failures
                        ],
                    )
                    round_summary["patch_count"] = len(patches)
                    if not patches:
                        termination = "no_progress"
                        round_summary["fix_failure_count"] = len(failures)
                        round_summary["blocked_issue_count"] = len(blocked_issues)
                        round_summary["termination"] = termination
                        debug.write_json("patches.json", [])
                        debug.write_json("fix_failures.json", failures)
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break

                    issue_keys_by_id = {
                        str(issue["issue_id"]): str(issue["issue_key"])
                        for issue in latest.issues
                        if isinstance(issue.get("issue_id"), str)
                        and isinstance(issue.get("issue_key"), str)
                    }
                    candidate_overrides = dict(target_overrides)
                    applicable: list[ProvisionalPatch] = []
                    hash_failures: list[dict[str, Any]] = []
                    for patch in patches:
                        location = (patch.chapter, patch.index)
                        current = evidence.segment_ref(*location)
                        if (
                            current is None
                            or ReviewFixer.target_hash(current.target) != patch.before_hash
                        ):
                            failure = {
                                "patch_id": patch.patch_id,
                                "issue_ids": list(patch.issue_ids),
                                "chapter": patch.chapter,
                                "index": patch.index,
                                "status": "failed",
                                "reason": "before_hash_changed",
                                "review_round": review_round,
                            }
                            fix_failures.append(failure)
                            failures.append(failure)
                            hash_failures.append(failure)
                            continue
                        candidate_overrides[location] = patch.after
                        applicable.append(patch)

                    register_blocked(
                        latest.issues,
                        hash_failures,
                    )
                    round_summary["fix_failure_count"] = len(failures)
                    round_summary["blocked_issue_count"] = len(blocked_issues)
                    candidate_digest = _review_overlay_digest(
                        loaded,
                        candidate_overrides,
                    )
                    debug.write_json(
                        "patches.json",
                        [patch.as_dict() for patch in patches],
                    )
                    debug.write_json("fix_failures.json", failures)
                    round_summary["candidate_overlay_digest"] = candidate_digest
                    round_summary["applicable_patch_count"] = len(applicable)
                    if not applicable or candidate_digest == overlay_digest:
                        termination = "no_progress"
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break
                    if candidate_digest in seen_overlays:
                        termination = "cycle_detected"
                        for patch in applicable:
                            record = {
                                **patch.as_dict(),
                                "issue_keys": sorted(
                                    {
                                        issue_keys_by_id[issue_id]
                                        for issue_id in patch.issue_ids
                                        if issue_id in issue_keys_by_id
                                    }
                                ),
                                "status": "rejected_cycle",
                            }
                            patch_records.append(record)
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        _save_checkpoint(review_round)
                        break

                    fix_rounds += 1
                    for patch in applicable:
                        location = (patch.chapter, patch.index)
                        previous = active_patches.get(location)
                        record = patch.as_dict()
                        record["issue_keys"] = sorted(
                            {
                                issue_keys_by_id[issue_id]
                                for issue_id in patch.issue_ids
                                if issue_id in issue_keys_by_id
                            }
                        )
                        if previous is not None:
                            previous["status"] = "superseded"
                            previous["superseded_by"] = patch.patch_id
                        patch_records.append(record)
                        active_patches[location] = record
                    target_overrides = candidate_overrides
                    seen_overlays.add(candidate_digest)
                    round_summary["fix_round"] = fix_rounds
                    debug.write_json("summary.json", round_summary)
                    round_summaries.append(round_summary)
                    _save_checkpoint(review_round)
            else:
                termination = "max_rounds"
                _save_checkpoint(max_review_rounds)

            if latest is None:  # pragma: no cover - max_review_rounds 至少为 1
                raise RuntimeError("Review loop finished without a review round")

            unresolved = effective_issues(latest)
            final_conflicts = _review_unresolved_conflict_records(unresolved)
            final_residual_conflicts = [
                record
                for record in final_conflicts
                if record.get("arbitration", {}).get("status") == "unresolved"
            ]
            final_fallback_agent_count = _review_unresolved_fallback_count(unresolved)
            initial_issues, dismissed = debug.result_snapshots()
            debug.write_json("rounds/final/initial_issues.json", initial_issues)
            debug.write_json("rounds/final/dismissed_issues.json", dismissed)
            debug.write_json(
                "rounds/final/pre_arbitration_issues.json",
                latest.pre_arbitration_issues,
            )
            debug.write_json(
                "rounds/final/arbitration_superseded_issues.json",
                latest.arbitration_superseded,
            )
            debug.write_json(
                "rounds/final/residual_conflicts.json",
                [
                    {
                        "conflict_id": record["conflict_id"],
                        "consistency_key": record["consistency_key"],
                        "issue_ids": record["issue_ids"],
                    }
                    for record in final_residual_conflicts
                ],
            )
            debug.write_json("rounds/final/conflicts.json", final_conflicts)
            debug.write_json("rounds/final/patch-history.json", patch_records)
            debug.write_json(
                "rounds/final/not_rereported_patches.json",
                [patch for patch in patch_records if patch["status"] == "not_rereported"],
            )
            debug.write_json(
                "rounds/final/unresolved_issues.json",
                unresolved,
            )
            debug.write_json("rounds/final/fix_failures.json", fix_failures)
            debug.write_json("rounds/final/rounds.json", round_summaries)
            debug.write_json(
                "rounds/final/shadow_targets.json",
                [
                    {
                        "chapter": chapter,
                        "index": index,
                        "target": target,
                    }
                    for (chapter, index), target in sorted(target_overrides.items())
                ],
            )
            public_issues = _review_public_issues(unresolved)
            changes = _review_net_changes(
                loaded,
                target_overrides,
                patch_records,
                active_patches,
            )
            summary = {
                "initial_issue_count": len(initial_issues),
                "dismissed_issue_count": len(dismissed),
                "pre_arbitration_issue_count": len(latest.pre_arbitration_issues),
                "arbitration_superseded_count": len(latest.arbitration_superseded),
                "issue_count": len(public_issues),
                "conflict_count": len(final_conflicts),
                "unresolved_conflict_count": len(final_residual_conflicts),
                "fallback_agent_count": final_fallback_agent_count,
                "review_round_count": len(round_summaries),
                "fix_round_count": fix_rounds,
                "patch_count": len(patch_records),
                "change_count": len(changes),
                "not_rereported_patch_count": sum(
                    patch["status"] == "not_rereported" for patch in patch_records
                ),
                "shadow_override_count": len(target_overrides),
                "blocked_issue_count": len(blocked_issues),
                "clean_streak": clean_streak,
            }
            debug.write_json("rounds/final/summary.json", summary)
            result = debug.finish(
                status="completed",
                termination=termination,
                summary=summary,
                issues=public_issues,
                changes=changes,
            )
            usage = save_review_usage()
            store.log_event(
                "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="completed",
                termination=termination,
                issue_count=len(public_issues),
                change_count=len(changes),
            )
            return ReviewOutcome(
                run_dir=debug.run_dir,
                result=result,
                usage=usage,
            )
        except Exception as error:
            initial_issues, dismissed = debug.result_snapshots()
            partial_issues = effective_issues(latest) if latest is not None else []
            public_issues = _review_public_issues(partial_issues)
            partial_changes = _review_net_changes(
                loaded,
                target_overrides,
                patch_records,
                active_patches,
            )
            debug.write_json("rounds/final/initial_issues.json", initial_issues)
            debug.write_json("rounds/final/dismissed_issues.json", dismissed)
            debug.write_json(
                "rounds/final/partial_issues.json",
                partial_issues,
            )
            debug.write_json("rounds/final/partial_patches.json", patch_records)
            debug.write_json("rounds/final/fix_failures.json", fix_failures)
            debug.finish(
                status="failed",
                termination="error",
                summary={
                    "issue_count": len(public_issues),
                    "change_count": len(partial_changes),
                    "conflict_count": (len(latest.conflict_groups) if latest is not None else 0),
                    "fallback_agent_count": (
                        latest.fallback_agent_count if latest is not None else 0
                    ),
                },
                issues=public_issues,
                changes=partial_changes,
                error={"type": type(error).__name__, "message": str(error)},
            )
            save_review_usage()
            store.log_event(
                "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="failed",
                termination="error",
                issue_count=len(public_issues),
                change_count=len(partial_changes),
                error_type=type(error).__name__,
                error=str(error),
            )
            raise

    def review_chapter(
        self,
        text_segs,
        terms,
        *,
        chapter_index: int | None = None,
        evidence: BookEvidenceIndex | None = None,
        debug: ReviewRunStore | None = None,
        target_overrides: Mapping[tuple[int, int], str] | None = None,
        review_round: int | None = None,
        on_chunk_finished: Callable[[int], None] | None = None,
    ) -> list[dict]:
        """把一章切成连续块并行审校，返回映射到章内段号的问题。

        块 = 连续段序列（约 3 倍翻译批大小，减少调用次数与重复注入的输入 token）；
        块内 reviewer 返回的 index 是块内下标，加块首段偏移映射回章内段号；
        越界 index 直接丢弃（模型幻觉防御）。各块只读固定译文和术语快照，
        可并行调用；结构化输出畸形时递归拆半，单段按配置有限重试；
        结果始终按原块顺序合并，保持确定性。
        """
        budget = self._runtime.config.segment.max_chars_per_batch * 3
        chunks = self.pack_contiguous(text_segs, budget)
        if not chunks:
            return []

        jobs: list[tuple[int, list]] = []
        base = 0
        for chunk in chunks:
            jobs.append((base, chunk))
            base += len(chunk)

        recovery_events: list[dict[str, Any]] = []
        recovery_lock = Lock()

        def record_recovery(event: str, **data: Any) -> None:
            """线程安全地暂存恢复事件，待并行任务结束后由主线程写日志。"""
            with recovery_lock:
                recovery_events.append({"event": event, **data})

        def review_once(chunk_base: int, chunk: list, *, attempt: int = 1) -> list[dict]:
            """调用一次审校，并把合法块内索引映射为章内索引。"""
            srcs = [s.source for s in chunk]
            overrides = target_overrides or {}

            def target_for(local_index: int, segment) -> str:
                """读取本轮影子译文；无章位置时回退正式译文。"""
                if chapter_index is None:
                    return segment.target or ""
                return overrides.get(
                    (chapter_index, chunk_base + local_index),
                    segment.target or "",
                )

            tgts = [target_for(local_index, segment) for local_index, segment in enumerate(chunk)]

            # 断点续跑：chunk 缓存检查（跳过 reviewer + agent loop LLM 调用）
            round_prefix = f"r{review_round}-" if review_round is not None else ""
            chunk_id = f"{round_prefix}ch{chapter_index}-base{chunk_base}-n{len(chunk)}"
            if debug is not None and debug.is_chunk_done(chunk_id):
                review_debug = debug
                cached = review_debug.load_chunk_result(chunk_id)
                if cached is not None:
                    # 恢复聚合状态（report 需要 initial/dismissed 数据）
                    if chapter_index is not None:
                        review_debug.record_initial_issues(
                            chapter=chapter_index,
                            chunk_base=chunk_base,
                            issues=cached.get("initial_issues", []),
                        )
                        review_debug.record_dismissed(
                            chapter=chapter_index,
                            chunk_base=chunk_base,
                            issues=cached.get("dismissed", []),
                        )
                    return cached.get("issues", [])

            # 断点续跑：子 chunk 缓存探测（借鉴翻译 _resume_batches 边界切分）
            # 与 review_adaptive 的递归拆半对齐：如果所有子 chunk 都有缓存，
            # 直接合并返回，避免触发一次 reviewer LLM 调用。
            if debug is not None and len(chunk) > 1:
                cached_sub = self._try_cached_subchunks(
                    chunk_base,
                    chunk,
                    debug,
                    round_prefix,
                    chapter_index,
                )
                if cached_sub is not None:
                    return cached_sub

            local_issues: list[dict] = []
            initial_trace: dict[str, Any] | None = None
            initial_path = ""
            initial_issue_count = 0
            repaired = False
            reused_initial: dict[str, Any] | None = None
            if debug is not None:
                round_prefix = f"r{review_round}-" if review_round is not None else ""
                initial_id = (
                    f"initial-{round_prefix}ch{chapter_index}-base{chunk_base}"
                    f"-n{len(chunk)}-attempt{attempt}"
                )
                initial_path = f"initial/{initial_id}.json"
                # 断点续跑：初筛已完成的结果直接复用（跳过最贵的 reviewer 调用）。
                # 结果在首次运行中已校验过块内 index，可直接信任。
                existing_initial = debug.load_json(initial_path)
                if (
                    existing_initial is not None
                    and existing_initial.get("status") == "finished"
                    and isinstance(existing_initial.get("issues"), list)
                ):
                    reused_initial = existing_initial
                else:
                    initial_trace = {
                        "agent_id": initial_id,
                        "chapter": chapter_index,
                        "chunk_base": chunk_base,
                        "segment_count": len(chunk),
                        "attempt": attempt,
                        "status": "running",
                    }
                    debug.write_json(initial_path, initial_trace)

            if reused_initial is not None:
                # 复用路径与 fresh 路径的错误语义不同：越界 index 不再抛
                # ReviewOutputError，而是静默丢弃。这有意为之——首次运行已
                # 校验并通过，越界只可能来自人工篡改；宁可少报也不让恢复
                # 因陈旧 trace 把整个 chunk 打成 fallback。会话级内容指纹
                # 守卫已排除正常内容变化场景。
                local_issues = [dict(issue) for issue in reused_initial["issues"]]
                repaired = bool(reused_initial.get("json_repaired"))
                if repaired:
                    record_recovery(
                        "review_json_repaired",
                        start_index=chunk_base,
                        count=len(chunk),
                    )
                # reused_initial 仅在 debug 非空时赋值；显式收窄供类型检查。
                if debug is not None and chapter_index is not None:
                    debug.record_initial_issues(
                        chapter=chapter_index,
                        chunk_base=chunk_base,
                        issues=local_issues,
                    )
                initial_issue_count = len(local_issues)
            else:

                def trace(event: str, data: dict[str, Any]) -> None:
                    """逐步保存初审完整请求、原始响应或服务错误。"""
                    if debug is None or initial_trace is None:
                        return
                    initial_trace[event] = data
                    debug.write_json(initial_path, initial_trace)

                try:
                    review_result = self._runtime.reviewer.review_result(
                        srcs,
                        tgts,
                        terms,
                        trace=trace if debug is not None else None,
                    )
                except Exception as error:
                    if debug is not None and initial_trace is not None:
                        initial_trace["status"] = "failed"
                        initial_trace["error"] = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                        debug.write_json(initial_path, initial_trace)
                    raise
                repaired = review_result.repaired
                if repaired:
                    record_recovery(
                        "review_json_repaired",
                        start_index=chunk_base,
                        count=len(chunk),
                    )
                for it in review_result.issues:
                    it = dict(it)
                    idx = it.get("index")
                    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(chunk):
                        it["index"] = idx
                        local_issues.append(it)
                    else:
                        raise ReviewOutputError("invalid_issue_index")
                initial_issue_count = len(review_result.issues)
                if debug is not None and initial_trace is not None:
                    initial_trace["status"] = "finished"
                    initial_trace["json_repaired"] = repaired
                    initial_trace["issues"] = local_issues
                    debug.write_json(initial_path, initial_trace)
                    if chapter_index is not None:
                        debug.record_initial_issues(
                            chapter=chapter_index,
                            chunk_base=chunk_base,
                            issues=local_issues,
                        )

            # 保存 agent loop 前的初始 issues（供 chunk 缓存落盘）
            local_issues_before_agent = list(local_issues)
            dismissed: list[dict[str, Any]] = []
            fallback_reason = ""
            if (
                local_issues
                and evidence is not None
                and debug is not None
                and self._runtime.config.pipeline.review_agent_loop
                and chapter_index is not None
            ):
                outcome = ReviewAgentLoop(
                    self._runtime.client,
                    self._runtime.config,
                    evidence,
                    debug,
                ).review_chunk(
                    chapter=chapter_index,
                    chunk_base=chunk_base,
                    sources=srcs,
                    targets=tgts,
                    initial_issues=local_issues,
                    review_round=review_round,
                )
                local_issues = outcome.issues
                dismissed = outcome.dismissed
                fallback_reason = outcome.fallback_reason
                debug.record_dismissed(
                    chapter=chapter_index,
                    chunk_base=chunk_base,
                    issues=dismissed,
                )

            mapped: list[dict[str, Any]] = []
            for issue in local_issues:
                local_index = issue.get("index")
                if (
                    isinstance(local_index, int)
                    and not isinstance(local_index, bool)
                    and 0 <= local_index < len(chunk)
                ):
                    issue = dict(issue)
                    issue["index"] = chunk_base + local_index
                    issue["_chunk_id"] = chunk_id
                    if fallback_reason:
                        issue["fallback_reason"] = fallback_reason
                    mapped.append(issue)
            if debug is not None:
                debug.log_event(
                    "review_leaf_finished",
                    chapter=chapter_index,
                    chunk_base=chunk_base,
                    segment_count=len(chunk),
                    initial_issue_count=initial_issue_count,
                    final_issue_count=len(mapped),
                    dismissed_count=len(dismissed),
                    fallback=bool(fallback_reason),
                )
            # 断点续跑：落盘 chunk 结果（在 review_once 内，有完整数据）
            if debug is not None and chapter_index is not None:
                debug.mark_chunk_done(
                    chunk_id,
                    {
                        "issues": mapped,
                        "initial_issues": local_issues_before_agent,
                        "dismissed": dismissed,
                        "fallback_reason": fallback_reason,
                    },
                )
            return mapped

        def review_adaptive(chunk_base: int, chunk: list) -> list[dict]:
            """畸形输出时缩小请求；单段仍失败才进行有限同输入重试。"""
            try:
                return review_once(chunk_base, chunk)
            except ReviewOutputError as error:
                if len(chunk) > 1:
                    mid = len(chunk) // 2
                    record_recovery(
                        "review_chunk_split",
                        start_index=chunk_base,
                        count=len(chunk),
                        left_count=mid,
                        right_count=len(chunk) - mid,
                        reason=error.reason,
                    )
                    return review_adaptive(chunk_base, chunk[:mid]) + review_adaptive(
                        chunk_base + mid, chunk[mid:]
                    )

                last_error = error
                retries = self._runtime.config.pipeline.review_output_retries
                for attempt in range(1, retries + 1):
                    record_recovery(
                        "review_singleton_retry",
                        start_index=chunk_base,
                        count=1,
                        attempt=attempt,
                        max_retries=retries,
                        reason=last_error.reason,
                    )
                    try:
                        result = review_once(chunk_base, chunk, attempt=attempt + 1)
                    except ReviewOutputError as retry_error:
                        last_error = retry_error
                        continue
                    record_recovery(
                        "review_singleton_recovered",
                        start_index=chunk_base,
                        count=1,
                        attempt=attempt,
                    )
                    return result
                record_recovery(
                    "review_singleton_failed",
                    start_index=chunk_base,
                    count=1,
                    attempts=retries + 1,
                    reason=last_error.reason,
                )
                raise last_error

        def review_one(job: tuple[int, list]) -> list[dict]:
            """审校一个初始连续块，并在必要时执行局部恢复。"""
            chunk_base, chunk = job
            return review_adaptive(chunk_base, chunk)

        workers = min(
            max(1, self._runtime.config.pipeline.review_concurrency),
            len(jobs),
        )
        try:
            if workers == 1:
                results = []
                for job in jobs:
                    results.append(review_one(job))
                    if on_chunk_finished:
                        on_chunk_finished(len(job[1]))
            else:
                ordered_results: list[list[dict] | None] = [None] * len(jobs)
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {
                        ex.submit(review_one, job): (position, len(job[1]))
                        for position, job in enumerate(jobs)
                    }
                    for future in as_completed(futures):
                        position, segment_count = futures[future]
                        ordered_results[position] = future.result()
                        if on_chunk_finished:
                            on_chunk_finished(segment_count)
                results = [result for result in ordered_results if result is not None]
        finally:
            if debug is not None:
                with recovery_lock:
                    event_order = {
                        "review_json_repaired": 0,
                        "review_chunk_split": 0,
                        "review_singleton_retry": 1,
                        "review_singleton_recovered": 2,
                        "review_singleton_failed": 2,
                    }
                    pending_events = sorted(
                        recovery_events,
                        key=lambda row: (
                            row.get("start_index", -1),
                            -row.get("count", 0),
                            event_order.get(row.get("event", ""), 99),
                            row.get("attempt", 0),
                        ),
                    )
                for row in pending_events:
                    event = row["event"]
                    payload = {
                        "chapter": chapter_index,
                        **{key: value for key, value in row.items() if key != "event"},
                    }
                    debug.log_event(event, **payload)
        return [issue for chunk_issues in results for issue in chunk_issues]

    @staticmethod
    def _try_cached_subchunks(
        chunk_base: int,
        chunk: list,
        debug: ReviewRunStore,
        round_prefix: str,
        chapter_index: int | None,
    ) -> list[dict] | None:
        """递归探测子 chunk 缓存，与 review_adaptive 的拆半对齐。

        翻译的 ``_resume_batches`` 按完成状态边界切分批，只补译缺失段。
        借鉴此模式：当父 chunk 缓存未命中时，递归检查所有子 chunk 是否有
        缓存。如果全部命中，合并返回，跳过 reviewer LLM 调用。

        探测阶段不写 initial/dismissed 快照；仅整棵子树命中后才统一落盘，
        避免半边命中返回 None 后父块重跑导致重复计数。
        """
        hits: list[tuple[int, dict[str, Any]]] = []

        def probe(base: int, pieces: list) -> list[dict] | None:
            if not pieces:
                return []
            chunk_id = f"{round_prefix}ch{chapter_index}-base{base}-n{len(pieces)}"
            if debug.is_chunk_done(chunk_id):
                cached = debug.load_chunk_result(chunk_id)
                if cached is not None:
                    hits.append((base, cached))
                    return list(cached.get("issues", []))
            if len(pieces) <= 1:
                return None
            mid = len(pieces) // 2
            left = probe(base, pieces[:mid])
            if left is None:
                return None
            right = probe(base + mid, pieces[mid:])
            if right is None:
                return None
            return left + right

        merged = probe(chunk_base, chunk)
        if merged is None:
            return None
        if chapter_index is not None:
            for base, cached in hits:
                debug.record_initial_issues(
                    chapter=chapter_index,
                    chunk_base=base,
                    issues=cached.get("initial_issues", []),
                )
                debug.record_dismissed(
                    chapter=chapter_index,
                    chunk_base=base,
                    issues=cached.get("dismissed", []),
                )
        return merged

    @staticmethod
    def pack_contiguous(segs, budget: int) -> list[list]:
        """按源文字符预算把段保序打包成若干连续块。"""
        chunks: list[list] = []
        cur: list = []
        size = 0
        for s in segs:
            if cur and size + len(s.source) > budget:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(s)
            size += len(s.source)
        if cur:
            chunks.append(cur)
        return chunks
