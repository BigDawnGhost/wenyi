"""跨章一致性 QA（廉价档）。

汇总术语表 + 各章译文摘要，让模型扫描术语译法漂移、代词性别不一致、语气文体漂移。
摘要只取每章首尾若干段并截断，控制 token。
"""

from __future__ import annotations

from typing import Any

from ..glossary.store import GlossaryStore
from ..pipeline.runstore import RunStore, STATUS_DONE
from . import prompts
from .base import Agent


class ConsistencyChecker(Agent):
    @staticmethod
    def _chapter_label(title: str, index: int) -> str:
        title = (title or "").strip()
        return title or f"章节 {index + 1}"

    def _chapter_digests(self, store: RunStore, max_chars_each: int = 600) -> str:
        m = store.load_manifest()
        parts: list[str] = []
        for c in m["chapters"]:
            if c["status"] != STATUS_DONE:
                continue
            ch = store.load_chapter(c["index"])
            targets = [s.target or "" for s in ch.text_segments]
            head = targets[:3]
            tail = targets[-2:] if len(targets) > 3 else []
            snippet = "……".join([t for t in head + tail if t])[:max_chars_each]
            parts.append(
                f"[{self._chapter_label(str(c.get('title', '')), int(c['index']))}]\n{snippet}"
            )
        return "\n\n".join(parts)

    def check(self, store: RunStore, glossary: GlossaryStore) -> list[dict[str, Any]]:
        digests = self._chapter_digests(store)
        if not digests.strip():
            return []
        system = prompts.render("consistency_system", src=self.src, tgt=self.tgt)
        user = (
            "【专有名词对照表】\n"
            + prompts.render_glossary(glossary.all_terms())
            + "\n\n【各章译文摘要】\n"
            + digests
            + '\n\n请输出 JSON：{"issues":[...]}。'
        )
        return self.dict_items(
            self._ask_json(system, user, tier="cheap", key="issues", default=[]))

    def autofix(self, store: RunStore, glossary: GlossaryStore) -> dict[str, Any]:
        """对可安全机械修复的术语/译名不一致，生成确定替换并改写正文。

        代词/语气类不在此处理（留作建议，避免重写句子损伤质量）。
        返回 {"replacements":[...], "rewritten": 改动段数}。
        """
        from .glossary_auditor import GlossaryAuditor

        digests = self._chapter_digests(store)
        if not digests.strip():
            return {"replacements": [], "rewritten": 0}
        system = prompts.render("consistency_fix_system", src=self.src, tgt=self.tgt)
        user = (
            "【专有名词对照表】\n"
            + prompts.render_glossary(glossary.all_terms())
            + "\n\n【各章译文摘要】\n" + digests
            + '\n\n请输出 JSON：{"replacements":[...]}。'
        )
        raw = self._ask_json(system, user, tier="strong", key="replacements", default=[])
        replace_map: dict[str, str] = {}
        applied: list[dict] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            wrong = str(r.get("wrong", "")).strip()
            right = str(r.get("right", "")).strip()
            if wrong and right and wrong != right:
                replace_map[wrong] = right
                applied.append({"wrong": wrong, "right": right, "reason": r.get("reason", "")})
        rewritten = GlossaryAuditor._rewrite_targets(store, glossary, replace_map) if replace_map else 0
        return {"replacements": applied, "rewritten": rewritten}
