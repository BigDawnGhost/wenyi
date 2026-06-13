"""跨章一致性 QA（廉价档）。

汇总术语表 + 各章译文摘要，让模型扫描术语译法漂移、代词性别不一致、语气文体漂移。
摘要只取每章首尾若干段并截断，控制 token。
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..glossary.store import GlossaryStore
from ..llm.base import LLMClient
from ..pipeline.runstore import RunStore, STATUS_DONE
from . import prompts


class ConsistencyChecker:
    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config

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
            parts.append(f"[第{c['index']}章 {c['title']}]\n{snippet}")
        return "\n\n".join(parts)

    def check(self, store: RunStore, glossary: GlossaryStore) -> list[dict[str, Any]]:
        digests = self._chapter_digests(store)
        if not digests.strip():
            return []
        system = prompts.render("consistency_system",
                                src=self.config.source_lang, tgt=self.config.target_lang)
        user = (
            "【专有名词对照表】\n"
            + prompts.render_glossary(glossary.all_terms())
            + "\n\n【各章译文摘要】\n"
            + digests
            + '\n\n请输出 JSON：{"issues":[...]}。'
        )
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tier="cheap",
            )
        except Exception:
            return []
        issues = data.get("issues", []) if isinstance(data, dict) else (data or [])
        return [i for i in issues if isinstance(i, dict)]
