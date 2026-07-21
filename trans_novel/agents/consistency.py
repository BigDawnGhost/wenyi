"""跨章一致性 QA（廉价档）。

汇总术语表 + 各章译文摘要，让模型扫描术语译法漂移、代词性别不一致、语气文体漂移。
摘要只取每章首尾若干段并截断，控制 token。
"""

from __future__ import annotations

from typing import Any

from .. import languages
from ..glossary.store import GlossaryStore
from ..pipeline.runstore import RunStore, STATUS_DONE
from . import prompts
from .base import Agent


class ConsistencyChecker(Agent):
    @staticmethod
    def _chapter_label(title: str, index: int, *, target: str = "zh") -> str:
        """返回适合报告展示的章节名，无标题时按序号生成。"""
        return languages.chapter_label(title or "", index, target=target)

    def _chapter_digests(self, store: RunStore, max_chars_each: int = 600) -> str:
        """提取已完成章节的首尾译文，拼成受字符预算限制的检查摘要。"""
        m = store.load_manifest()
        parts: list[str] = []
        for c in m["chapters"]:
            if c["status"] != STATUS_DONE:
                continue
            ch = store.load_chapter(c["index"])
            targets = [s.target or "" for s in ch.text_segments]
            head = targets[:3]
            tail = targets[-2:] if len(targets) > 3 else []
            snippet = languages.join_digest_segments(
                [text for text in head + tail if text],
                target=self.tgt,
            )[:max_chars_each]
            label = self._chapter_label(
                str(c.get("title", "")),
                int(c["index"]),
                target=self.tgt,
            )
            parts.append(
                languages.chapter_digest(label, snippet, target=self.tgt)
            )
        return "\n\n".join(parts)

    def check(self, store: RunStore, glossary: GlossaryStore) -> list[dict[str, Any]]:
        """结合术语表和各章摘要扫描跨章一致性问题。"""
        digests = self._chapter_digests(store)
        if not digests.strip():
            return []
        system = prompts.render("consistency_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "consistency_user",
            src=self.src,
            tgt=self.tgt,
            glossary=prompts.render_glossary(
                glossary.all_terms(),
                tgt=self.tgt,
            ),
            digests=digests,
        )
        return self.dict_items(
            self._ask_json(system, user, tier="cheap", key="issues", default=[]))
