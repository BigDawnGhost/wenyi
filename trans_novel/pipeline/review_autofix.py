"""Review Autofix 发布服务。

现有 Review 引擎仍保持只读：本服务在 Review 结果完整落盘后，先把
``changes`` 叠加到不可变工作快照，再让最终未解决 ``issues`` 按段进入有界取证
Agent Loop。全部候选准备完成后先写 ``autofix/index.json``，然后才修改正式
章节 ``target``，使进程中断后可按 before/after 哈希幂等续跑。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from ..agents.review_fixer import ProvisionalPatch, ReviewFixer
from ..agents.review_loop import ReviewAgentLoop
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..llm.usage import usage_delta
from ..review.evidence import BookEvidenceIndex
from ..review.run_store import ReviewOutcome, ReviewRunStore, review_candidate_id
from .docx_styles import DocxStyleService

if TYPE_CHECKING:
    from .annotations import AnnotationService
    from .runstore import RunStore
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


def _sha256(text: str) -> str:
    """返回 Autofix 乐观写回协议使用的 UTF-8 SHA-256。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_json(path: str) -> Any:
    """读取 Review 目录内已有 JSON；调用方负责容错。"""
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def _integer_index(value: Any) -> int | None:
    """只接受非布尔整数位置，并向静态类型检查器显式收窄类型。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class ReviewAutofixService:
    """把只读 Review 结果发布到正式章节，并保留可恢复索引。"""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._annotations = annotations
        self._docx_styles = DocxStyleService(runtime)

    @staticmethod
    def _review_result(run_dir: str) -> dict[str, Any] | None:
        """读取一次 Review 的结果对象。"""
        try:
            result = _load_json(os.path.join(run_dir, "result.json"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _index(run_dir: str) -> dict[str, Any] | None:
        """读取 Autofix 索引；缺失或损坏时返回 None。"""
        try:
            index = _load_json(os.path.join(run_dir, "autofix", "index.json"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return index if isinstance(index, dict) else None

    def resume_pending(
        self,
        store: RunStore,
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome | None:
        """优先完成已生成发布索引的中断 Autofix，避免重跑 Review/Agent。"""
        if not os.path.isdir(store.reviews_dir):
            return None
        for name in sorted(os.listdir(store.reviews_dir), reverse=True):
            if not name.startswith("review-"):
                continue
            run_dir = os.path.join(store.reviews_dir, name)
            result = self._review_result(run_dir)
            if result is None:
                continue
            index = self._index(run_dir)
            # 只处理最新的有效 Review。若它尚未生成索引，正常路径会先复用
            # 该 Review 结果再规划 Autofix；不得越过它发布更早的遗留索引。
            if index is None:
                return None
            status = index.get("status")
            result_autofix = result.get("autofix")
            if status == "applying":
                debug = ReviewRunStore.open_existing(run_dir)
                debug.log_event("review_autofix_resumed", review_id=name)
                return self._apply_index(store, debug, index, result, progress=progress)
            if status in {"completed", "partial"} and not isinstance(result_autofix, dict):
                debug = ReviewRunStore.open_existing(run_dir)
                return self._finish_result(store, debug, index, result)
            # 新目录在前；它若已完成，更早的索引不应再发布。
            if status in {"completed", "partial"}:
                return None
        return None

    def run(
        self,
        store: RunStore,
        outcome: ReviewOutcome,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """为已完成 Review 生成终局候选、索引并幂等发布。"""
        if not self._runtime.config.pipeline.review_autofix:
            return outcome
        debug = ReviewRunStore.open_existing(outcome.run_dir)
        existing = self._index(outcome.run_dir)
        if existing is not None:
            status = existing.get("status")
            if status == "applying":
                return self._apply_index(
                    store,
                    debug,
                    existing,
                    outcome.result,
                    progress=progress,
                )
            if status in {"completed", "partial"}:
                return self._finish_result(store, debug, existing, outcome.result)

        manifest = store.load_manifest()
        chapters = [
            store.load_chapter(row["index"])
            for row in manifest.get("chapters", [])
            if isinstance(row.get("index"), int)
        ]
        chapters_by_index = {chapter.index: chapter for chapter in chapters}
        analysis = store.load_analysis() or {}
        records: list[dict[str, Any]] = []
        overrides: dict[tuple[int, int], str] = {}

        def add_record(
            *,
            chapter: int,
            index: int,
            segment_ref: str,
            origin: str,
            before: str,
            after: str | None,
            issue_keys: list[str] | None = None,
            issue_ids: list[str] | None = None,
            review_result: str = "",
            status: str = "planned",
            reason: str = "",
            evidence_refs: list[str] | None = None,
            source_change: dict[str, Any] | None = None,
            related_issues: list[dict[str, Any]] | None = None,
            artifacts: list[str] | None = None,
        ) -> dict[str, Any]:
            record = {
                "record_id": f"autofix-{len(records) + 1:05d}",
                "chapter": chapter,
                "index": index,
                "segment_ref": segment_ref,
                "origin": origin,
                "before": before,
                "before_hash": _sha256(before),
                "after": after,
                "after_hash": _sha256(after) if isinstance(after, str) else None,
                "issue_keys": sorted({item for item in issue_keys or [] if item}),
                "issue_ids": sorted({item for item in issue_ids or [] if item}),
                "review_result": review_result,
                "status": status,
            }
            if reason:
                record["reason"] = reason
            if evidence_refs:
                record["evidence_refs"] = sorted(set(evidence_refs))
            if source_change is not None:
                record["source_change"] = dict(source_change)
            if related_issues:
                record["related_issues"] = [dict(issue) for issue in related_issues]
            if artifacts:
                record["artifacts"] = sorted(set(artifacts))
            records.append(record)
            return record

        # changes 先全量叠加。用户显式开启 Autofix 时，不再对 review_result
        # 做二次筛选；原始状态仍保留在索引供后续解析。
        def location_key(item: dict[str, Any]) -> tuple[int, int]:
            chapter = _integer_index(item.get("chapter"))
            index = _integer_index(item.get("index"))
            return (chapter if chapter is not None else -1, index if index is not None else -1)

        changes = sorted(
            (dict(change) for change in outcome.changes if isinstance(change, dict)),
            key=location_key,
        )
        for change in changes:
            chapter_index = _integer_index(change.get("chapter"))
            text_index = _integer_index(change.get("index"))
            suggested = change.get("suggested_target")
            chapter = chapters_by_index.get(chapter_index) if chapter_index is not None else None
            if (
                chapter is None
                or text_index is None
                or not 0 <= text_index < len(chapter.text_segments)
                or not isinstance(suggested, str)
                or not suggested.strip()
            ):
                add_record(
                    chapter=chapter_index if chapter_index is not None else -1,
                    index=text_index if text_index is not None else -1,
                    segment_ref="",
                    origin="change",
                    before="",
                    after=None,
                    issue_keys=[
                        str(key) for key in change.get("issue_keys", []) if isinstance(key, str)
                    ],
                    review_result=str(change.get("review_result") or ""),
                    status="failed",
                    reason="invalid_change",
                    source_change=change,
                )
                continue
            segment = chapter.text_segments[text_index]
            location = (chapter.index, text_index)
            before = overrides.get(location, segment.target or "")
            after = suggested
            add_record(
                chapter=chapter.index,
                index=text_index,
                segment_ref=f"ch{chapter.index}:text{text_index}:seg{segment.index}",
                origin="change",
                before=before,
                after=after,
                issue_keys=[
                    str(key) for key in change.get("issue_keys", []) if isinstance(key, str)
                ],
                review_result=str(change.get("review_result") or ""),
                status="not_applied_no_net_change" if after == before else "planned",
                reason="unchanged_target" if after == before else "",
                source_change=change,
            )
            if after != before:
                overrides[location] = after

        evidence = BookEvidenceIndex(
            chapters,
            all_terms,
            analysis,
            target_overrides=overrides,
        )
        issues_path = os.path.join(outcome.run_dir, "rounds", "final", "unresolved_issues.json")
        try:
            raw_issues = _load_json(issues_path)
        except (OSError, json.JSONDecodeError, TypeError):
            raw_issues = outcome.issues
        if not isinstance(raw_issues, list):
            raw_issues = outcome.issues
        issues = [dict(issue) for issue in raw_issues if isinstance(issue, dict)]
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for issue in issues:
            chapter_index = issue.get("chapter")
            text_index = issue.get("index")
            if (
                isinstance(chapter_index, int)
                and not isinstance(chapter_index, bool)
                and isinstance(text_index, int)
                and not isinstance(text_index, bool)
                and chapter_index in chapters_by_index
                and 0 <= text_index < len(chapters_by_index[chapter_index].text_segments)
            ):
                issue.setdefault("issue_id", issue.get("issue_key"))
                grouped.setdefault((chapter_index, text_index), []).append(issue)
                continue
            add_record(
                chapter=(
                    chapter_index
                    if isinstance(chapter_index, int) and not isinstance(chapter_index, bool)
                    else -1
                ),
                index=(
                    text_index
                    if isinstance(text_index, int) and not isinstance(text_index, bool)
                    else -1
                ),
                segment_ref="",
                origin="final_issue_fix",
                before="",
                after=None,
                issue_keys=[str(issue.get("issue_key") or "")],
                issue_ids=[str(issue.get("issue_id") or issue.get("issue_key") or "")],
                status="failed",
                reason="invalid_issue_location",
                related_issues=[issue],
            )

        jobs = sorted(grouped.items())
        if progress and jobs:
            progress(0, len(jobs), "终局自动修订")
        review_agent = ReviewAgentLoop(
            self._runtime.client,
            self._runtime.config,
            evidence,
            debug,
        )
        fixer = ReviewFixer(self._runtime.client, self._runtime.config)
        style = self._runtime.analyzer.style_brief(analysis)
        book_synopsis = str(analysis.get("book_synopsis", "") or "")
        fixer_round = max(
            1,
            int((outcome.result.get("summary") or {}).get("review_round_count") or 0) + 1,
        )

        def fix_job(
            job: tuple[tuple[int, int], list[dict[str, Any]]],
        ) -> dict[str, Any]:
            location, location_issues = job
            segment = evidence.segment_ref(*location)
            if segment is None:
                return {
                    "location": location,
                    "original_issues": location_issues,
                    "verified_issues": [],
                    "dismissed": [],
                    "patch": None,
                    "status": "failed",
                    "reason": "segment_not_found",
                }

            localized = [{**dict(issue), "index": 0} for issue in location_issues]
            candidates = {
                review_candidate_id(location[0], location[1], ordinal, fixer_round): issue
                for ordinal, issue in enumerate(location_issues)
            }
            loop_outcome = review_agent.review_chunk(
                chapter=location[0],
                chunk_base=location[1],
                sources=[segment.source],
                targets=[segment.target],
                initial_issues=localized,
                review_round=fixer_round,
            )
            if loop_outcome.fallback_reason:
                return {
                    "location": location,
                    "original_issues": location_issues,
                    "verified_issues": [],
                    "dismissed": [],
                    "patch": None,
                    "status": "failed",
                    "reason": f"review_agent_fallback:{loop_outcome.fallback_reason}",
                }

            verified: list[dict[str, Any]] = []
            for ordinal, issue in enumerate(loop_outcome.issues):
                mapped = dict(issue)
                candidate_id = mapped.get("candidate_id")
                original = candidates.get(str(candidate_id))
                if original is not None:
                    issue_id = str(
                        original.get("issue_id") or original.get("issue_key") or candidate_id
                    )
                    issue_key = str(original.get("issue_key") or issue_id)
                else:
                    identity = json.dumps(
                        [
                            location[0],
                            location[1],
                            mapped.get("type"),
                            mapped.get("detail"),
                            mapped.get("suggestion"),
                            ordinal,
                        ],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    issue_id = f"autofix-agent-{_sha256(identity)[:16]}"
                    issue_key = issue_id
                mapped.update(
                    {
                        "issue_id": issue_id,
                        "issue_key": issue_key,
                        "chapter": location[0],
                        "index": location[1],
                    }
                )
                verified.append(mapped)

            dismissed: list[dict[str, Any]] = []
            for item in loop_outcome.dismissed:
                candidate_id = str(item.get("candidate_id") or "")
                original = candidates.get(candidate_id)
                if original is None:
                    continue
                dismissed.append(
                    {
                        **dict(item),
                        "issue_id": str(
                            original.get("issue_id") or original.get("issue_key") or candidate_id
                        ),
                        "issue_key": str(original.get("issue_key") or candidate_id),
                        "chapter": location[0],
                        "index": location[1],
                    }
                )
            if not verified:
                return {
                    "location": location,
                    "original_issues": location_issues,
                    "verified_issues": [],
                    "dismissed": dismissed,
                    "patch": None,
                    "status": "dismissed",
                    "reason": "all_issues_dismissed",
                }

            context = evidence.segment_context(
                {
                    "chapter": location[0],
                    "index": location[1],
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
            trace_path = f"autofix/fixers/ch{location[0]}-text{location[1]}.json"
            existing_trace = debug.load_json(trace_path)
            if isinstance(existing_trace, dict):
                cached_patch = existing_trace.get("patch")
                if existing_trace.get("status") == "finished" and isinstance(cached_patch, dict):
                    try:
                        patch = ProvisionalPatch(
                            patch_id=str(cached_patch["patch_id"]),
                            round=int(cached_patch["round"]),
                            segment_ref=str(cached_patch["segment_ref"]),
                            chapter=int(cached_patch["chapter"]),
                            index=int(cached_patch["index"]),
                            before_hash=str(cached_patch["before_hash"]),
                            before=str(cached_patch["before"]),
                            after=str(cached_patch["after"]),
                            issue_ids=tuple(str(item) for item in cached_patch["issue_ids"]),
                        )
                    except (KeyError, TypeError, ValueError):
                        patch = None
                    if patch is not None:
                        return {
                            "location": location,
                            "original_issues": location_issues,
                            "verified_issues": verified,
                            "dismissed": dismissed,
                            "patch": patch,
                            "status": "fixed",
                            "reason": "",
                        }
                if existing_trace.get("status") == "failed":
                    cached_error = existing_trace.get("error") or {}
                    return {
                        "location": location,
                        "original_issues": location_issues,
                        "verified_issues": verified,
                        "dismissed": dismissed,
                        "patch": None,
                        "status": "failed",
                        "reason": str(cached_error.get("message") or "cached_fixer_failure"),
                    }
            trace: dict[str, Any] = {
                "chapter": location[0],
                "index": location[1],
                "segment_ref": segment.ref,
                "status": "running",
            }
            debug.write_json(trace_path, trace)

            def record(event: str, data: dict[str, Any]) -> None:
                trace[event] = data
                debug.write_json(trace_path, trace)

            try:
                patch = fixer.propose(
                    fixer_round,
                    segment.ref,
                    location[0],
                    location[1],
                    segment.source,
                    segment.target,
                    verified,
                    style=style,
                    book_synopsis=book_synopsis,
                    chapter_digest=evidence.chapter_digests.get(location[0], ""),
                    relevant_glossary=relevant_terms,
                    nearby_pairs=nearby_pairs,
                    trace=record,
                )
            except Exception as error:  # noqa: BLE001 - 终局失败保留未修改问题
                trace["status"] = "failed"
                trace["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                debug.write_json(trace_path, trace)
                return {
                    "location": location,
                    "original_issues": location_issues,
                    "verified_issues": verified,
                    "dismissed": dismissed,
                    "patch": None,
                    "status": "failed",
                    "reason": f"{type(error).__name__}:{error}",
                }
            trace["status"] = "finished"
            trace["patch"] = patch.as_dict()
            debug.write_json(trace_path, trace)
            return {
                "location": location,
                "original_issues": location_issues,
                "verified_issues": verified,
                "dismissed": dismissed,
                "patch": patch,
                "status": "fixed",
                "reason": "",
            }

        workers = min(max(1, self._runtime.config.pipeline.review_concurrency), len(jobs))
        if workers <= 1:
            fixed = []
            for done, job in enumerate(jobs, start=1):
                fixed.append(fix_job(job))
                if progress:
                    progress(done, len(jobs), "终局自动修订")
        else:
            ordered: list[dict[str, Any] | None] = [None] * len(jobs)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(fix_job, job): position for position, job in enumerate(jobs)
                }
                for done, future in enumerate(as_completed(futures), start=1):
                    ordered[futures[future]] = future.result()
                    if progress:
                        progress(done, len(jobs), "终局自动修订")
            fixed = [item for item in ordered if item is not None]

        for fixed_result in fixed:
            location = fixed_result["location"]
            location_issues = fixed_result["original_issues"]
            verified_issues = fixed_result["verified_issues"]
            dismissed = fixed_result["dismissed"]
            patch: ProvisionalPatch | None = fixed_result["patch"]
            reason = str(fixed_result.get("reason") or "")
            current = evidence.segment_ref(*location)
            before = current.target if current is not None else ""
            agent_artifact = (
                f"agents/r{fixer_round}-chunk-ch{location[0]}-base{location[1]}-n1.json"
            )
            fixer_artifact = f"autofix/fixers/ch{location[0]}-text{location[1]}.json"
            if dismissed:
                add_record(
                    chapter=location[0],
                    index=location[1],
                    segment_ref=current.ref if current is not None else "",
                    origin="final_issue_review",
                    before=before,
                    after=None,
                    issue_keys=[
                        str(issue.get("issue_key") or "")
                        for issue in dismissed
                        if str(issue.get("issue_key") or "")
                    ],
                    issue_ids=[
                        str(issue.get("issue_id") or issue.get("issue_key") or "")
                        for issue in dismissed
                        if str(issue.get("issue_id") or issue.get("issue_key") or "")
                    ],
                    status="dismissed",
                    reason="dismissed_by_review_agent_loop",
                    related_issues=dismissed,
                    artifacts=[agent_artifact],
                )
            if fixed_result.get("status") == "dismissed":
                continue
            if patch is None:
                add_record(
                    chapter=location[0],
                    index=location[1],
                    segment_ref=current.ref if current is not None else "",
                    origin="final_issue_fix",
                    before=before,
                    after=None,
                    issue_keys=[
                        str(issue.get("issue_key") or "")
                        for issue in (verified_issues or location_issues)
                        if str(issue.get("issue_key") or "")
                    ],
                    issue_ids=[
                        str(issue.get("issue_id") or issue.get("issue_key") or "")
                        for issue in (verified_issues or location_issues)
                        if str(issue.get("issue_id") or issue.get("issue_key") or "")
                    ],
                    status="failed",
                    reason=reason or "review_fixer_failed",
                    related_issues=verified_issues or location_issues,
                    artifacts=[
                        agent_artifact,
                        *([fixer_artifact] if verified_issues else []),
                    ],
                )
                continue
            add_record(
                chapter=location[0],
                index=location[1],
                segment_ref=patch.segment_ref,
                origin="final_issue_fix",
                before=patch.before,
                after=patch.after,
                issue_keys=[
                    str(issue.get("issue_key") or "")
                    for issue in verified_issues
                    if str(issue.get("issue_key") or "")
                ],
                issue_ids=list(patch.issue_ids),
                status="planned",
                reason="confirmed_by_review_agent_loop",
                evidence_refs=[
                    str(ref)
                    for issue in verified_issues
                    for ref in issue.get("evidence_refs", [])
                    if isinstance(ref, str)
                ],
                related_issues=verified_issues,
                artifacts=[agent_artifact, fixer_artifact],
            )
            overrides[location] = patch.after

        locations: list[dict[str, Any]] = []
        for (chapter_index, text_index), target in sorted(overrides.items()):
            segment = chapters_by_index[chapter_index].text_segments[text_index]
            baseline = segment.target or ""
            locations.append(
                {
                    "chapter": chapter_index,
                    "index": text_index,
                    "segment_ref": f"ch{chapter_index}:text{text_index}:seg{segment.index}",
                    "before": baseline,
                    "before_hash": _sha256(baseline),
                    "target": target,
                    "target_hash": _sha256(target),
                    "record_ids": [
                        record["record_id"]
                        for record in records
                        if record["chapter"] == chapter_index
                        and record["index"] == text_index
                        and record["status"] == "planned"
                    ],
                    "status": "pending",
                    "alignment_status": "pending",
                }
            )

        index = {
            "version": 1,
            "review_id": debug.review_id,
            "status": "applying",
            "reviewed_content_digest": outcome.result.get("reviewed_content_digest"),
            "records": records,
            "locations": locations,
        }
        debug.write_json("autofix/index.json", index)
        debug.log_event(
            "review_autofix_planned",
            record_count=len(records),
            location_count=len(locations),
            issue_group_count=len(jobs),
        )
        self._save_usage_delta(store, debug, scope="review_autofix_agent")
        return self._apply_index(
            store,
            debug,
            index,
            outcome.result,
            progress=progress,
        )

    def _save_usage_delta(
        self,
        store: RunStore,
        debug: ReviewRunStore,
        *,
        scope: str,
    ) -> None:
        """把当前 Runtime 尚未落盘的调用同时合并到 Review 与全书账本。"""
        before = store.load_usage() or {
            "totals": {},
            "by_tier": {},
            "by_stage": {},
        }
        cumulative = self._runtime.flush_usage(store, scope=scope)
        increment = usage_delta(cumulative, before)
        if increment.get("totals", {}).get("calls"):
            debug.save_usage(increment)

    def _apply_index(
        self,
        store: RunStore,
        debug: ReviewRunStore,
        index: dict[str, Any],
        result: dict[str, Any],
        *,
        progress: ProgressFn | None,
    ) -> ReviewOutcome:
        """按索引幂等写回正式 target，然后刷新与字符位置相关的元数据。"""
        raw_locations = index.get("locations")
        locations = [row for row in raw_locations or [] if isinstance(row, dict)]
        by_chapter: dict[int, list[dict[str, Any]]] = {}
        for row in locations:
            chapter = row.get("chapter")
            if isinstance(chapter, int) and not isinstance(chapter, bool):
                by_chapter.setdefault(chapter, []).append(row)

        total = len(locations)
        done = 0
        if progress and total:
            progress(0, total, "写回 Review Autofix")
        for chapter_index in sorted(by_chapter):
            chapter = store.load_chapter(chapter_index)
            chapter_locations = sorted(
                by_chapter[chapter_index], key=lambda row: row.get("index", -1)
            )
            applied_positions: list[int] = []
            chapter_changed = False
            for row in chapter_locations:
                text_index = row.get("index")
                target = row.get("target")
                before = row.get("before")
                if (
                    isinstance(text_index, bool)
                    or not isinstance(text_index, int)
                    or not 0 <= text_index < len(chapter.text_segments)
                    or not isinstance(target, str)
                    or not isinstance(before, str)
                ):
                    row["status"] = "failed"
                    row["reason"] = "invalid_index_location"
                    done += 1
                    continue
                current = chapter.text_segments[text_index].target or ""
                if current == target:
                    row["status"] = "no_net_change" if current == before else "applied"
                    if current != before and row.get("alignment_status") != "completed":
                        applied_positions.append(text_index)
                elif current == before and _sha256(current) == row.get("before_hash"):
                    chapter.text_segments[text_index].target = target
                    row["status"] = "applied"
                    applied_positions.append(text_index)
                    chapter_changed = True
                else:
                    row["status"] = "failed"
                    row["reason"] = "formal_target_changed"
                    row["actual_hash"] = _sha256(current)
                done += 1
                if progress:
                    progress(done, total, "写回 Review Autofix")
            if chapter_changed:
                store.save_chapter(chapter)

            # 翻译后对齐依赖 target 字符偏移；Autofix 发布后必须按最终文本刷新。
            for text_index in sorted(set(applied_positions)):
                row = next(item for item in chapter_locations if item.get("index") == text_index)
                if row.get("status") == "failed" or row.get("alignment_status") == "completed":
                    continue
                self._annotations.align_annotations_after_batch(
                    chapter_index,
                    chapter,
                    text_index,
                    1,
                    store,
                )
                self._docx_styles.align_styles_after_batch(
                    chapter_index,
                    chapter,
                    text_index,
                    1,
                    store,
                )
                row["alignment_status"] = "completed"
            debug.write_json("autofix/index.json", index)

        location_status = {
            (row.get("chapter"), row.get("index")): row.get("status") for row in locations
        }
        for record in index.get("records", []):
            if not isinstance(record, dict) or record.get("status") != "planned":
                continue
            status = location_status.get((record.get("chapter"), record.get("index")))
            if status == "applied":
                record["status"] = "applied"
            elif status == "no_net_change":
                record["status"] = "not_applied_no_net_change"
            elif status == "failed":
                record["status"] = "not_applied"

        has_failures = any(
            isinstance(record, dict) and record.get("status") in {"failed", "not_applied"}
            for record in index.get("records", [])
        ) or any(row.get("status") == "failed" for row in locations)
        index["status"] = "partial" if has_failures else "completed"
        debug.write_json("autofix/index.json", index)
        self._save_usage_delta(store, debug, scope="review_autofix_publish")
        return self._finish_result(store, debug, index, result)

    def _finish_result(
        self,
        store: RunStore,
        debug: ReviewRunStore,
        index: dict[str, Any],
        result: dict[str, Any],
    ) -> ReviewOutcome:
        """将 Autofix 发布摘要幂等合并到 Review result.json。"""
        records = [record for record in index.get("records", []) if isinstance(record, dict)]
        locations = [row for row in index.get("locations", []) if isinstance(row, dict)]
        applied_locations = [row for row in locations if row.get("status") == "applied"]
        failed_issue_records = [
            record
            for record in records
            if record.get("origin") == "final_issue_fix"
            and record.get("status") in {"failed", "not_applied"}
        ]
        autofix = {
            "enabled": True,
            "status": index.get("status", "partial"),
            "index": "autofix/index.json",
            "applied_segment_count": len(applied_locations),
            "applied_change_count": sum(
                record.get("origin") == "change" and record.get("status") == "applied"
                for record in records
            ),
            "applied_issue_fix_count": sum(
                record.get("origin") == "final_issue_fix" and record.get("status") == "applied"
                for record in records
            ),
            "failed_issue_count": sum(
                max(1, len(record.get("issue_ids") or [])) for record in failed_issue_records
            ),
            "failed_record_count": sum(
                record.get("status") in {"failed", "not_applied"} for record in records
            ),
        }
        updated = dict(result)
        existing_autofix = updated.get("autofix")
        if (
            isinstance(existing_autofix, dict)
            and existing_autofix.get("index") == "autofix/index.json"
            and existing_autofix.get("status") == autofix["status"]
        ):
            return ReviewOutcome(
                run_dir=debug.run_dir,
                result=updated,
                usage=debug.load_usage() or {},
            )
        updated["summary"] = {
            **dict(result.get("summary") or {}),
            "autofix_applied_segment_count": autofix["applied_segment_count"],
            "autofix_failed_issue_count": autofix["failed_issue_count"],
        }
        updated["autofix"] = autofix
        debug.write_json("result.json", updated)
        debug.log_event("review_autofix_finished", **autofix)
        store.log_event(
            "review_autofix_finished",
            review_id=debug.review_id,
            status=autofix["status"],
            applied_segment_count=autofix["applied_segment_count"],
            failed_issue_count=autofix["failed_issue_count"],
        )
        return ReviewOutcome(
            run_dir=debug.run_dir,
            result=updated,
            usage=debug.load_usage() or {},
        )
