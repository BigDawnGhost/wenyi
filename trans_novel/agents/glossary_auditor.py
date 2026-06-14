"""术语 AI 审计与统一（收尾自动 pass）。

目标：消除同一专名的译法漂移（如 "Kaho" 在正文里 佳穂/佳穗 混用）。

流程：
1. 候选侦测：取术语表 + 已记录的译法冲突 + 在各章译文里扫描"与术语译法形近(汉字编辑距离 1)"的变体；
2. 强档模型裁定每个原文词的【规范译法 canonical】与应被替换的变体；
3. 落地：锁定术语表 canonical、变体并入别名、标记冲突已解决；
   并**改写各章已译正文**（变体→canonical），同步翻译记忆库。
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..glossary.store import GlossaryStore, GlossaryTerm, TYPE_PERSON
from ..llm.base import LLMClient
from ..pipeline.runstore import RunStore
from . import prompts


def _is_cjk(s: str) -> bool:
    return bool(s) and all("一" <= c <= "鿿" for c in s)


def _hamming1_variants(target: str, corpus: str) -> set[str]:
    """在 corpus 中找与 target 等长、仅差 1 个汉字、且确为汉字串的形近变体。"""
    L = len(target)
    if L < 2 or not _is_cjk(target):
        return set()
    found: set[str] = set()
    seen: set[str] = set()
    for i in range(len(corpus) - L + 1):
        w = corpus[i : i + L]
        if w in seen or w == target:
            continue
        seen.add(w)
        if not _is_cjk(w):
            continue
        if sum(1 for a, b in zip(w, target) if a != b) == 1:
            found.add(w)
    return found


class GlossaryAuditor:
    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang

    # ── 候选侦测 ────────────────────────────────────────────────────────────
    def _candidates(self, store: RunStore, glossary: GlossaryStore) -> dict[str, dict[str, Any]]:
        terms = glossary.all_terms()
        corpus = self._target_corpus(store)
        cand: dict[str, dict[str, Any]] = {}
        for t in terms:
            variants: set[str] = set()
            # 形近变体仅对人名/术语等汉字译名扫描，控制噪声
            if t.type == TYPE_PERSON or _is_cjk(t.target):
                variants |= _hamming1_variants(t.target, corpus)
            if variants:
                cand[t.source] = {
                    "source": t.source, "current": t.target,
                    "type": t.type, "variants": sorted(variants),
                }
        # 已记录的译法冲突也并入候选
        for c in glossary.open_conflicts():
            src = c["source"]
            entry = cand.setdefault(src, {"source": src, "current": c.get("existing_target", ""),
                                          "type": "", "variants": []})
            for v in (c.get("existing_target"), c.get("proposed_target")):
                if v and v != entry["current"] and v not in entry["variants"]:
                    entry["variants"].append(v)
        return cand

    @staticmethod
    def _target_corpus(store: RunStore) -> str:
        m = store.load_manifest()
        parts: list[str] = []
        for c in m["chapters"]:
            ch = store.load_chapter(c["index"])
            parts.extend(s.target or "" for s in ch.text_segments)
        return "\n".join(parts)

    # ── 模型裁定 ────────────────────────────────────────────────────────────
    def _decide(self, candidates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        if not candidates:
            return []
        lines = []
        for c in candidates.values():
            allv = [c["current"]] + [v for v in c["variants"] if v != c["current"]]
            lines.append(f"- {c['source']}（{c['type'] or '?'}）: 现有译法/变体 = {', '.join(allv)}")
        user = (
            "下列原文词在术语表或正文里出现了多种译法/形近变体，请为每个裁定唯一规范译法：\n"
            + "\n".join(lines)
            + '\n\n输出 JSON：{"unifications":[{"source":"...","canonical":"...","variants":["..."],"reason":"..."}]}'
        )
        system = prompts.render("glossary_audit_system", src=self.src, tgt=self.tgt)
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tier="strong",
            )
        except Exception:
            return []
        uni = data.get("unifications", []) if isinstance(data, dict) else (data or [])
        return [u for u in uni if isinstance(u, dict) and u.get("source") and u.get("canonical")]

    # ── 落地 ────────────────────────────────────────────────────────────────
    def audit(self, store: RunStore, glossary: GlossaryStore) -> list[dict[str, Any]]:
        """返回已应用的统一记录列表。"""
        candidates = self._candidates(store, glossary)
        unifications = self._decide(candidates)
        applied: list[dict[str, Any]] = []
        # 收集 变体→规范 的全局替换表（仅汉字变体，避免误伤）
        replace_map: dict[str, str] = {}
        for u in unifications:
            src = str(u["source"])
            canonical = str(u["canonical"]).strip()
            variants = [str(v).strip() for v in u.get("variants", []) if str(v).strip()]
            variants = [v for v in variants if v and v != canonical]
            if not canonical:
                continue
            # 术语表：锁定 canonical，变体并入别名，标记冲突已解决
            glossary.lock_term(src, canonical)
            if variants:
                glossary.upsert_term(
                    GlossaryTerm(source=src, target=canonical, aliases=variants,
                                 confidence="high", locked=True),
                )
            glossary.mark_conflicts_resolved(src)
            for v in variants:
                if _is_cjk(v):           # 仅对汉字变体做正文替换，安全
                    replace_map[v] = canonical
            applied.append({"source": src, "canonical": canonical,
                            "variants": variants, "reason": u.get("reason", "")})

        if replace_map:
            self._rewrite_targets(store, glossary, replace_map)
        return applied

    @staticmethod
    def _rewrite_targets(store: RunStore, glossary: GlossaryStore,
                         replace_map: dict[str, str]) -> int:
        """把各章 target 里的变体替换为规范译法，并同步 TM。返回改动段数。"""
        # 长变体优先替换，避免短串先替导致嵌套问题
        variants_sorted = sorted(replace_map, key=len, reverse=True)

        def _apply(text: str) -> str:
            if not text:
                return text
            for v in variants_sorted:
                if v in text:
                    text = text.replace(v, replace_map[v])
            return text

        m = store.load_manifest()
        changed = 0
        for c in m["chapters"]:
            ch = store.load_chapter(c["index"])
            dirty = False
            for seg in ch.segments:
                if not seg.target:
                    continue
                new = _apply(seg.target)
                if new != seg.target:
                    seg.target = new
                    dirty = True
                    changed += 1
                    glossary.add_tm(seg.source, new, c["index"])
            if dirty:
                store.save_chapter(ch)

        # 同步改写已译的书名/章节标题，保持目录与输出文件名一致
        man_dirty = False
        nt = _apply(m.get("title_translated"))
        if nt != m.get("title_translated"):
            m["title_translated"] = nt
            man_dirty = True
        for c in m["chapters"]:
            ct = _apply(c.get("title_translated"))
            if ct != c.get("title_translated"):
                c["title_translated"] = ct
                man_dirty = True
        if man_dirty:
            store.save_manifest(m)
        return changed
