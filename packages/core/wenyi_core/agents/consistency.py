"""跨章一致性 QA（廉价档）。

汇总术语表 + 各章译文摘要，让模型扫描术语译法漂移、代词性别不一致、语气文体漂移。
摘要只取每章首尾若干段并截断，控制 token。
"""

from __future__ import annotations

from typing import Any

from ..storage import STATUS_DONE, Storage
from . import prompts
from .base import Agent


class ConsistencyChecker(Agent):
    @staticmethod
    def _chapter_label(title: str, index: int) -> str:
        title = (title or "").strip()
        return title or f"章节 {index + 1}"

    def _chapter_digests(self, storage: Storage, max_chars_each: int = 600) -> str:
        m = storage.load_manifest()
        parts: list[str] = []
        for c in m["chapters"]:
            if c["status"] != STATUS_DONE:
                continue
            ch = storage.load_chapter(c["index"])
            targets = [s.target or "" for s in ch.text_segments]
            head = targets[:3]
            tail = targets[-2:] if len(targets) > 3 else []
            snippet = "……".join([t for t in head + tail if t])[:max_chars_each]
            parts.append(
                f"[{self._chapter_label(str(c.get('title', '')), int(c['index']))}]\n{snippet}"
            )
        return "\n\n".join(parts)

    def check(self, storage: Storage) -> list[dict[str, Any]]:
        digests = self._chapter_digests(storage)
        if not digests.strip():
            return []
        system = prompts.render("consistency_system", src=self.src, tgt=self.tgt)
        user = (
            "【专有名词对照表】\n"
            + prompts.render_glossary(storage.all_terms())
            + "\n\n【各章译文摘要】\n"
            + digests
            + '\n\n请输出 JSON：{"issues":[...]}。'
        )
        return self.dict_items(
            self._ask_json(system, user, tier="cheap", key="issues", default=[]))
