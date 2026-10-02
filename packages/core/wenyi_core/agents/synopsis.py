"""Book-understanding prescan agent using an economical tier.
Read the source before translation. Store one target-language digest per chapter in
chapter.meta["source_digest"], then combine digests and preliminary analysis into a
whole-book synopsis.
Inject both as a stable prompt prefix so translators know the plot, character arcs,
foreshadowing and revelations before translating early chapters. The fixed global prefix
supports cache reuse. Use grouped map-reduce merging for long books to bound prompt length.
"""

from __future__ import annotations

from .base import Agent

# Character budget for one digest merge; group and recursively merge larger inputs.
_REDUCE_BUDGET = 12000


class Synopsizer(Agent):
    policy_phase = "analysis"

    def digest_chapter(self, source_text: str) -> str:
        """Summarize one source chapter in the target language; return empty on empty input or
        failure.
        """
        if not source_text.strip():
            return ""
        system = self.render("chapter_digest_system", src=self.src, tgt=self.tgt)
        user = self.render(
            "chapter_digest_user", src=self.src, tgt=self.tgt, source=source_text[:8000]
        )
        # Use the fast tier with output headroom above the language-specific digest budget.
        return self._ask_text(system, user, operation="synopsis.chapter")

    def book_synopsis(self, digests: list[str], analysis_brief: str) -> str:
        """Combine chapter digests and analysis into a book synopsis; use map-reduce for long
        inputs.
        """
        items = [d.strip() for d in digests if d and d.strip()]
        if not items:
            return ""
        while True:
            groups = self._group(items, _REDUCE_BUDGET)
            if len(groups) == 1:
                return self._synth(groups[0], analysis_brief)
            # Summarize each group first, then merge those summaries in the next round.
            summaries = []
            for group in groups:
                summary = self._synth(group, analysis_brief)
                if not summary:
                    # Every group is required; dropping one would hide missing chapters on resume.
                    return ""
                summaries.append(summary)
            items = summaries

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

    def _synth(self, digests: list[str], analysis_brief: str) -> str:
        """Merge one group of chapter digests and style analysis into a higher-level synopsis."""
        numbered = "\n".join(f"[{i}] {d}" for i, d in enumerate(digests))
        system = self.render("book_synopsis_system", src=self.src, tgt=self.tgt)
        user = self.render(
            "book_synopsis_user",
            src=self.src,
            tgt=self.tgt,
            analysis=analysis_brief or "(none)",
            digests=numbered,
        )
        # Shared transport retries truncation; optional synopsis failures keep an empty fallback.
        return self._ask_text(system, user, operation="synopsis.book")
