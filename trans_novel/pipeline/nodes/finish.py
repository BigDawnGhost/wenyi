"""书级收尾节点：标题、确定性 QA、报告与装配。

- titles：章标题与目录项翻译，幂等且严格校验模型输出；
- deterministic_qa：对所有已完成章节执行确定性 lint，不调用模型；
- report：QA 报告；
- assemble：正式回填，就绪门禁拒绝不完整状态。
"""

from __future__ import annotations

from trans_novel.agents import prompts
from trans_novel.agents.base import WorkflowProtocolError, retry_protocol
from trans_novel.assemble import assemble_outputs, bilingual_out_path
from trans_novel.assemble.report import build_report
from trans_novel.config import Config, OutputConfig
from trans_novel.glossary.store import GlossaryStore, terms_matching_text
from trans_novel.ingest import KIND_HEADING, preserved_toc_entry_ids
from trans_novel.ingest.models import sanitize_generated_text
from trans_novel.pipeline.contracts import NodeOutcome, NodeRequest
from trans_novel.pipeline.planning import (
    analyst_model_profile,
    assemble_input_fingerprint,
    deterministic_qa_input_fingerprint,
    glossary_semantic_fingerprint_part,
    report_input_fingerprint,
    titles_input_fingerprint,
)
from trans_novel.pipeline.quality import lint_targets
from trans_novel.pipeline.state import (
    NODE_ASSEMBLE,
    NODE_DETERMINISTIC_QA,
    NODE_REPORT,
    NODE_TITLES,
    SCOPE_BOOK,
)


def _toc_state(store, manifest):
    chapters = manifest.get("chapters", [])
    raw_meta = manifest.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_entries = meta.get("toc_entries", [])
    toc_entries = raw_entries if isinstance(raw_entries, list) else []
    stored = [store.load_chapter(chapter["index"]) for chapter in chapters]
    preserved_chapters = {chapter.index for chapter in stored if chapter.preserve_source}
    preserved_entries = preserved_toc_entry_ids(stored, toc_entries)
    entries_by_id = {
        entry["entry_id"]: entry
        for entry in toc_entries
        if isinstance(entry, dict) and isinstance(entry.get("entry_id"), str)
    }
    return chapters, toc_entries, entries_by_id, preserved_chapters, preserved_entries


class TitlesNode:
    """章标题/目录项翻译（schema 2 TOC entry 驱动；幂等）。"""

    node_id = NODE_TITLES
    scope = SCOPE_BOOK

    def __init__(
        self,
        *,
        client,
        config: Config,
        src: str,
        tgt: str,
        glossary: GlossaryStore,
    ):
        self.client = client
        self.config = config
        self.src = src
        self.tgt = tgt
        self.glossary = glossary

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        if store.pending_chapters():
            return NodeOutcome()  # 还有未完成章 → 不译标题（与旧 run() 条件一致）
        m = store.load_manifest()
        (
            chapters,
            toc_entry_list,
            entries_by_id,
            preserved_chapters,
            preserved_entries,
        ) = _toc_state(store, m)
        for chapter in chapters:
            if chapter.get("index") in preserved_chapters:
                chapter.pop("title_translated", None)

        def _flat(s: object) -> str:
            return " ".join(str(s or "").split())

        for entry_id in preserved_entries:
            entries_by_id[entry_id].pop("title_translated", None)
        toc_entries_pending = [
            e
            for e in toc_entry_list
            if isinstance(e, dict)
            and not e.get("external")
            and e.get("entry_id") not in preserved_entries
            and _flat(e.get("title", ""))
            and not e.get("title_translated")
        ]

        toc_covered_chapters = []
        other_chapters = []
        for c in chapters:
            if c.get("index") in preserved_chapters:
                continue
            entry_id = c.get("toc_entry_id")
            if entry_id and entry_id in entries_by_id:
                toc_covered_chapters.append(c)
            elif _flat(c.get("title", "")):
                other_chapters.append(c)

        m.pop("title_translated", None)

        llm_chapters = []
        for c in other_chapters:
            chapter = store.load_chapter(c["index"])
            segs = chapter.segments
            heading_target = ""
            if (
                segs
                and segs[0].kind == KIND_HEADING
                and _flat(segs[0].source) == _flat(c.get("title", ""))
            ):
                heading_target = _flat(segs[0].target)
            if heading_target:
                c["title_translated"] = heading_target
            elif not c.get("title_translated"):
                llm_chapters.append(c)

        for c in toc_covered_chapters:
            entry_translated = entries_by_id[c["toc_entry_id"]].get("title_translated")
            if entry_translated:
                c["title_translated"] = entry_translated

        store.save_manifest(m)  # 先落盘复用/同步结果，即便后续 LLM 调用失败也不丢失

        if not llm_chapters and not toc_entries_pending:
            store.log_event("titles_skipped", reason="already_translated")
            fp = self._fingerprint(store)
            return NodeOutcome(fingerprint=fp)

        pending_items: list[dict] = [*llm_chapters, *toc_entries_pending]
        unique_titles = list(dict.fromkeys(_flat(item.get("title", "")) for item in pending_items))

        if request.progress:
            request.progress(0, 0, "翻译章节标题…")
        translated_titles: dict[str, str] = {}
        for title in unique_titles:
            translated_titles[title] = retry_protocol(
                lambda title=title: self._translate_title(title, self.src, self.tgt, store),
                retries=self.config.pipeline.protocol_retry_limit,
            )

        for item in pending_items:
            key = _flat(item.get("title", ""))
            item["title_translated"] = translated_titles[key]

        for c in toc_covered_chapters:
            entry_translated = entries_by_id[c["toc_entry_id"]].get("title_translated")
            if entry_translated:
                c["title_translated"] = entry_translated

        store.save_manifest(m)
        store.log_event(
            "titles_translated",
            titles=[
                {"index": i, "source": source, "target": translated_titles[source]}
                for i, source in enumerate(unique_titles)
            ],
        )
        fp = self._fingerprint(store)
        return NodeOutcome(fingerprint=fp)

    def _translate_title(self, title: str, src: str, tgt: str, store) -> str:
        system = prompts.render("title_translator_system", src=src, tgt=tgt, n=1)
        title_terms = terms_matching_text(self.glossary.all_terms(), title)
        user = prompts.render(
            "title_translator_user",
            src=src,
            tgt=tgt,
            glossary=prompts.render_glossary(title_terms),
            n=1,
            numbered_titles=prompts.numbered([title]),
        )
        data = self.client.complete_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            stage="title_translate",
            agent="analyst",
            operation="title.translate",
        )
        out = data.get("titles") if isinstance(data, dict) else data
        if not isinstance(out, list) or len(out) != 1:
            store.log_event(
                "titles_translation_rejected",
                reason="count_mismatch",
                expected=1,
                actual=len(out) if isinstance(out, list) else None,
            )
            raise WorkflowProtocolError("title_count_mismatch")
        translated = sanitize_generated_text(out[0]).strip() if isinstance(out[0], str) else ""
        if not translated:
            store.log_event(
                "titles_translation_rejected",
                reason="empty_item",
                expected=1,
            )
            raise WorkflowProtocolError("title_empty_item")
        return translated

    def _fingerprint(self, store) -> str:
        manifest = store.load_manifest()
        chapters, toc_entries, _, preserved_chapters, preserved_entries = _toc_state(
            store, manifest
        )
        translated = [
            chapter for chapter in chapters if chapter.get("index") not in preserved_chapters
        ]
        titles = [str(chapter.get("title", "")) for chapter in translated if chapter.get("title")]
        titles.extend(
            str(entry.get("title", ""))
            for entry in toc_entries
            if isinstance(entry, dict)
            and entry.get("title")
            and entry.get("entry_id") not in preserved_entries
        )
        identity = manifest.get("identity") if isinstance(manifest.get("identity"), dict) else {}
        src = identity.get("source_lang") or self.config.source_lang
        tgt = identity.get("target_lang") or self.config.target_lang
        return titles_input_fingerprint(titles, src, tgt, analyst_model_profile(self.config))


class DeterministicQANode:
    node_id = NODE_DETERMINISTIC_QA
    scope = SCOPE_BOOK

    def __init__(self, *, glossary: GlossaryStore):
        self.glossary = glossary

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        terms = [term for term in self.glossary.all_terms() if getattr(term, "locked", 0)]
        issues: list[dict] = []
        target_texts: list[str] = []
        state = store.load_state()
        for chapter_meta in state.chapters:
            if chapter_meta.processing is not None and chapter_meta.processing.action == "preserve":
                continue
            if store.load_progress(chapter_meta.index).status != "done":
                continue
            chapter = store.load_chapter(chapter_meta.index)
            target_texts.append(
                "\n".join(segment.target or "" for segment in chapter.text_segments)
            )
            segments = chapter.text_segments
            found = lint_targets(
                [segment.source for segment in segments],
                [segment.target or "" for segment in segments],
                locked_terms=terms,
                src_lang=state.identity.source_lang or "en",
            )
            for item in found:
                segment = segments[item.index]
                issues.append(
                    {
                        "chapter": chapter_meta.index,
                        "index": segment.index,
                        "type": item.type,
                        "detail": item.detail,
                    }
                )
        store.record_node_output(NODE_DETERMINISTIC_QA, {"issues": issues})
        fp = deterministic_qa_input_fingerprint(
            "\n".join(target_texts),
            glossary_semantic_fingerprint_part(terms),
        )
        return NodeOutcome(findings_count=len(issues), artifacts={"issues": issues}, fingerprint=fp)


class ReportNode:
    node_id = NODE_REPORT
    scope = SCOPE_BOOK

    def __init__(self, *, glossary: GlossaryStore):
        self.glossary = glossary

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        report = build_report(store, self.glossary)
        store.save_report(report)
        store.log_event("report_saved", path=store.report_path)
        return NodeOutcome(
            artifacts={"report": report},
            fingerprint=self._fingerprint(store, report["deterministic_issues"]),
        )

    def _fingerprint(self, store, deterministic: list | None = None) -> str:
        state = store.load_state()
        lint_issues = [issue for ci in state.progress.values() for issue in ci.lint_issues]
        titles = [c.title for c in state.chapters if c.title]
        if deterministic is None:
            deterministic = lint_issues
        return report_input_fingerprint(
            lint_issues, deterministic or [], [t.source for t in self.glossary.all_terms()], titles
        )


class AssembleNode:
    """正式回填（mono/bilingual 按输出配置）；就绪门禁在 writer.assemble 内部。"""

    node_id = NODE_ASSEMBLE
    scope = SCOPE_BOOK

    def __init__(
        self,
        *,
        output: OutputConfig,
        out_format: str = "epub",
        out_path: str | None = None,
        theme=None,
        output_digest: str | None = None,
    ):
        self.output = output
        self.out_format = out_format
        self.out_path = out_path
        self.theme = theme
        self.output_digest = output_digest

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        manifest = store.load_manifest()
        manifest_changed = False
        for chapter in manifest.get("chapters", []):
            if isinstance(chapter, dict) and isinstance(chapter.get("title_translated"), str):
                cleaned = sanitize_generated_text(chapter["title_translated"])
                if cleaned != chapter["title_translated"]:
                    chapter["title_translated"] = cleaned
                    manifest_changed = True
        meta = manifest.get("meta")
        toc_entries = meta.get("toc_entries", []) if isinstance(meta, dict) else []
        for entry in toc_entries:
            if isinstance(entry, dict) and isinstance(entry.get("title_translated"), str):
                cleaned = sanitize_generated_text(entry["title_translated"])
                if cleaned != entry["title_translated"]:
                    entry["title_translated"] = cleaned
                    manifest_changed = True
        if manifest_changed:
            store.save_manifest(manifest)

        if request.progress:
            request.progress(0, 0, "生成译文文件…")
        do_mono = self.output.mono
        do_bilingual = self.output.bilingual.enabled
        requests: list[tuple[str | None, bool]] = []
        if do_mono:
            requests.append((self.out_path, False))
        if do_bilingual:
            requests.append((bilingual_out_path(self.out_path) if self.out_path else None, True))
        outputs = assemble_outputs(
            store,
            request.input_path,
            requests,
            self.out_format,
            order=self.output.bilingual.order,
            theme=self.theme,
            output_digest=self.output_digest,
        )
        store.log_event("assembled", outputs=outputs, out_format=self.out_format)
        fp = assemble_input_fingerprint(
            self._targets_text(store),
            mono=do_mono,
            bilingual=do_bilingual,
            out_format=self.out_format,
            bilingual_order=self.output.bilingual.order,
            output_digest=self.output_digest,
        )
        return NodeOutcome(
            artifacts={"outputs": outputs, "output_digest": self.output_digest},
            fingerprint=fp,
        )

    @staticmethod
    def _targets_text(store) -> str:
        from trans_novel.pipeline.state import STATUS_DONE

        parts: list[str] = []
        for c in store.load_state().chapters:
            if store.load_progress(c.index).status != STATUS_DONE:
                continue
            parts.append(
                "\n".join(s.target or "" for s in store.load_chapter(c.index).text_segments)
            )
        return "\n".join(parts)


__all__ = ["AssembleNode", "DeterministicQANode", "ReportNode", "TitlesNode"]
