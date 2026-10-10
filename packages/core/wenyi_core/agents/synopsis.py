"""Book-understanding prescan agent using an economical tier.
Read the source before translation. Store one target-language digest per chapter in
chapter.meta["source_digest"], then combine digests and preliminary analysis into a
whole-book synopsis.
Inject both as a stable prompt prefix so translators know the plot, character arcs,
foreshadowing and revelations before translating early chapters. The fixed global prefix
supports cache reuse. Use grouped map-reduce merging for long books to bound prompt length.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ..glossary.store import GlossaryStore, GlossaryTerm
from .base import Agent

# Character budget for one digest merge; group and recursively merge larger inputs.
_REDUCE_BUDGET = 12000
_SOURCE_BUDGET = 8000


class Synopsizer(Agent):
    policy_phase = "analysis"

    def digest_chapter(self, source_text: str) -> str:
        """Cover every source block; publish nothing if any map or merge fails."""
        if not source_text.strip():
            return ""
        if not self.language_policy.enabled("terminology.context"):
            return self._digest(source_text[:_SOURCE_BUDGET])
        digests = []
        for start in range(0, len(source_text), _SOURCE_BUDGET):
            digest = self._digest(source_text[start : start + _SOURCE_BUDGET])
            if not digest or len(digest) > _REDUCE_BUDGET:
                return ""
            digests.append(digest)
        return self._reduce(digests, self._merge_digests)

    def _digest(self, source: str) -> str:
        system = self.render("chapter_digest_system", src=self.src, tgt=self.tgt)
        user = self.render("chapter_digest_user", src=self.src, tgt=self.tgt, source=source)
        # Use the fast tier with output headroom above the language-specific digest budget.
        return self._ask_text(system, user, operation="synopsis.chapter").strip()

    def _merge_digests(self, digests: list[str]) -> str:
        return self._digest(
            "[Ordered draft digests of consecutive parts of ONE chapter; merge all parts, "
            "preserving original-spelling identities and chronology. Do not invent translations.]\n"
            + "\n".join(digests)
        )

    def book_synopsis(
        self,
        digests: list[str],
        analysis_brief: str,
        glossary: list[GlossaryTerm] | None = None,
    ) -> str:
        """Combine chapter digests and analysis into a book synopsis; use map-reduce for long
        inputs.
        """
        items = [d.strip() for d in digests if d and d.strip()]
        if not items:
            return ""
        if not self.language_policy.enabled("terminology.context"):
            while True:
                groups = self._group(items, _REDUCE_BUDGET)
                if len(groups) == 1:
                    return self._synth(groups[0], analysis_brief)
                summaries = []
                for group in groups:
                    summary = self._synth(group, analysis_brief)
                    if not summary:
                        return ""
                    summaries.append(summary)
                items = summaries
        # Split oversized legacy/caller inputs without dropping their endings.
        items = [
            item[start : start + _REDUCE_BUDGET]
            for item in items
            for start in range(0, len(item), _REDUCE_BUDGET)
        ]
        return self._reduce(
            items, lambda group: self._synth(group, analysis_brief, glossary), synthesize_one=True
        )

    def _reduce(
        self,
        items: list[str],
        synthesize: Callable[[list[str]], str],
        *,
        synthesize_one: bool = False,
    ) -> str:
        """Bound every group and require strict progress to prevent runaway reductions."""
        if len(items) == 1 and not synthesize_one:
            return items[0]
        for _ in range(32):
            groups = self._group(items, _REDUCE_BUDGET)
            if len(groups) == 1:
                result = synthesize(groups[0]).strip()
                return result if len(result) <= _REDUCE_BUDGET else ""
            # Summarize each group first, then merge those summaries in the next round.
            summaries = []
            for group in groups:
                summary = synthesize(group).strip()
                if not summary or len(summary) > _REDUCE_BUDGET:
                    # Every group is required; dropping one would hide missing chapters on resume.
                    return ""
                summaries.append(summary)
            if (len(summaries), sum(map(len, summaries))) >= (
                len(items),
                sum(map(len, items)),
            ):
                return ""
            items = summaries
        return ""

    # Internal helpers.
    @staticmethod
    def _group(items: list[str], budget: int) -> list[list[str]]:
        """Greedily group strings by character budget, keeping joined groups near or below
        budget.
        """
        groups: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for it in items:
            if cur and size + len(it) > budget:
                groups.append(cur)
                cur, size = [], 0
            cur.append(it)
            size += len(it) + 1
        if cur:
            groups.append(cur)
        return groups

    def _synth(
        self,
        digests: list[str],
        analysis_brief: str,
        glossary: list[GlossaryTerm] | None = None,
    ) -> str:
        """Merge one group of chapter digests and style analysis into a higher-level synopsis."""
        numbered = "\n".join(f"[{i}] {d}" for i, d in enumerate(digests))
        enabled = self.language_policy.enabled("terminology.context")
        style_lines = []
        used = 0
        for line in analysis_brief.splitlines():
            if used + len(line) + 1 > 4000:
                style_lines.append("[Additional style context omitted by budget.]")
                break
            style_lines.append(line)
            used += len(line) + 1
        system = self.render("book_synopsis_system", src=self.src, tgt=self.tgt)
        user = self.render(
            "book_synopsis_user",
            src=self.src,
            tgt=self.tgt,
            analysis=("\n".join(style_lines) if enabled else analysis_brief) or "(none)",
            digests=numbered,
        )
        if not enabled:
            return self._ask_text(system, user, operation="synopsis.book")
        user += "\n\n[Relevant authoritative mappings: source, target, type]\n"
        used = 0
        for term in GlossaryStore.terms_in(glossary or [], "\n".join(digests)):
            line = json.dumps([term.source, term.target, term.type], ensure_ascii=False)
            if used + len(line) + 1 > 6000:
                user += "[Additional mappings omitted; retain source-spelling identities.]\n"
                break
            user += line + "\n"
            used += len(line) + 1
        # Shared transport retries truncation; optional synopsis failures keep an empty fallback.
        return self._ask_text(system, user, operation="synopsis.book")
