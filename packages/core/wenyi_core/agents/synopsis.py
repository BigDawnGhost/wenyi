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
from ..ingest.tokens import count_tokens, split_balanced_tokens
from .base import Agent


class Synopsizer(Agent):
    policy_phase = "analysis"

    def digest_chapter(self, source_text: str) -> str:
        """Cover every source block; publish nothing if any map or merge fails."""
        if not source_text.strip():
            return ""
        pipeline = self.config.pipeline
        digests = []
        for source in split_balanced_tokens(source_text, pipeline.synopsis_source_tokens):
            digest = self._digest(source)
            if not digest or count_tokens(digest) > pipeline.synopsis_reduce_tokens:
                return ""
            digests.append(digest)
        return self._reduce(digests, self._merge_digests)

    def _digest(self, source: str) -> str:
        system = self.render("chapter_digest_system", src=self.src, tgt=self.tgt)
        user = self.render("chapter_digest_user", src=self.src, tgt=self.tgt, source=source)
        # Use the fast tier without overriding its configured or provider-default output limit.
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
        # Split oversized legacy/caller inputs without dropping their endings.
        budget = self.config.pipeline.synopsis_reduce_tokens
        items = [chunk for item in items for chunk in split_balanced_tokens(item, budget)]
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
        budget = self.config.pipeline.synopsis_reduce_tokens
        for _ in range(32):
            groups = self._group(items, budget)
            if len(groups) == 1:
                result = synthesize(groups[0]).strip()
                return result if count_tokens(result) <= budget else ""
            # Summarize each group first, then merge those summaries in the next round.
            summaries = []
            for group in groups:
                summary = synthesize(group).strip()
                if not summary or count_tokens(summary) > budget:
                    # Every group is required; dropping one would hide missing chapters on resume.
                    return ""
                summaries.append(summary)
            if (len(summaries), sum(map(count_tokens, summaries))) >= (
                len(items),
                sum(map(count_tokens, items)),
            ):
                return ""
            items = summaries
        return ""

    # Internal helpers.
    @staticmethod
    def _group(items: list[str], budget: int) -> list[list[str]]:
        """Group intact digests in order, counting tokens in the newline-joined content."""
        groups: list[list[str]] = []
        cur: list[str] = []
        for it in items:
            if cur and count_tokens("\n".join([*cur, it])) > budget:
                groups.append(cur)
                cur = []
            cur.append(it)
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
        style_lines = []
        for line in analysis_brief.splitlines():
            if count_tokens("\n".join([*style_lines, line])) > 4000:
                style_lines.append("[Additional style context omitted by budget.]")
                break
            style_lines.append(line)
        system = self.render("book_synopsis_system", src=self.src, tgt=self.tgt)
        user = self.render(
            "book_synopsis_user",
            src=self.src,
            tgt=self.tgt,
            analysis="\n".join(style_lines) or "(none)",
            digests=numbered,
        )
        user += "\n\n[Relevant authoritative mappings: source, target, type]\n"
        mappings = ""
        for term in GlossaryStore.terms_in(glossary or [], "\n".join(digests)):
            line = json.dumps([term.source, term.target, term.type], ensure_ascii=False) + "\n"
            if count_tokens(mappings + line) > 6000:
                mappings += "[Additional mappings omitted; retain source-spelling identities.]\n"
                break
            mappings += line
        user += mappings
        # Shared transport retries truncation; optional synopsis failures keep an empty fallback.
        return self._ask_text(system, user, operation="synopsis.book")
