"""Parallel, term-scoped maintenance after glossary extraction; only the caller writes state."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from ...agents.glossary_extractor import TranslatedSegmentEvidence
from ...agents.glossary_note import GlossaryNoteAgent, NoteWindow
from ...glossary.store import GlossaryStore, source_matches_text, trusted_match_sources
from ...i18n.policy.models import content_hash
from ...llm.routing import inference_snapshot
from ...models.document import Segment
from ...models.glossary import GlossaryTerm
from ...storage.protocol import Storage


class GlossaryNotes:
    def __init__(
        self,
        agent: GlossaryNoteAgent,
        store: Storage,
        history: Mapping[tuple[int, int], TranslatedSegmentEvidence] | None = None,
    ):
        self.agent = agent
        self.store = store
        self.history = history if history is not None else {}
        self.identity = [
            3,
            agent.language_policy.task_fingerprint("glossary_note_system"),
            inference_snapshot(agent.config.llm, ("glossary.note",)),
            store.load_manifest().get("source_sha256"),
        ]

    def admit(self, term: GlossaryTerm, chapter: int | None) -> str:
        # The extractor's uncited candidate note is neither an editor note nor a fact.
        # Empty incoming notes preserve any existing editor text during upsert.
        with self.store.state_lock():
            current = self.store.get_term(term.source)
            if current and current.target != term.target and current.status == "conflict":
                if any(
                    row["source"] == term.source
                    and row["existing_target"] == current.target
                    and row["proposed_target"] == term.target
                    for row in self.store.open_conflicts()
                ):
                    return "conflict"
            return self.store.upsert_term(replace(term, note=""), chapter=chapter)

    def maintain(
        self,
        chapter: int,
        start: int,
        segments: list[Segment],
        *,
        exclude: set[str] | None = None,
    ) -> set[str]:
        window = NoteWindow(
            chapter, start, tuple((s.source, s.target or "") for s in segments), self.history
        )
        scope = "batch" if exclude is None else "fallback"
        digest = content_hash([self.identity, scope, chapter, start, window.pairs])
        root = f"glossary/notes/{digest}"
        complete = self.store.read_artifact(f"{root}/done.json")
        if complete is not None:
            return set(complete["terms"])
        terms = self.store.all_terms()
        relevant = GlossaryStore.terms_in(terms, "\n".join(source for source, _ in window.pairs))
        covered = set()
        jobs = []
        for term in relevant:
            if exclude is not None and term.source in exclude:
                continue
            keys = trusted_match_sources(term, terms)
            hits = [
                start + i
                for i, (source, _) in enumerate(window.pairs)
                if any(source_matches_text(k, source) for k in keys)
            ]
            if not hits:
                continue
            covered.add(term.source)
            key = f"{root}/{content_hash(term.source)}.json"
            prepared = self.store.read_artifact(key)
            if prepared is not None:
                if not prepared["published"] and prepared.get("outcome") != "not_adopted":
                    self._publish(key, term.source, prepared)
            else:
                jobs.append((key, term, hits))

        if jobs:
            with (
                self.agent.client.interrupt_scope(),
                ThreadPoolExecutor(
                    max_workers=min(len(jobs), self.agent.config.pipeline.glossary_note_workers)
                ) as pool,
            ):
                futures = [
                    pool.submit(self.agent.update, term, window, hits) for _, term, hits in jobs
                ]
                failed = None
                try:
                    # Do not let model completion order affect publication or SQLite ownership.
                    for (key, term, _), future in zip(jobs, futures):
                        try:
                            result = future.result()
                        except Exception as error:
                            # Checkpoint independent successes before surfacing the first failure.
                            if failed is None:
                                failed = error
                            continue
                        record = {
                            "before": self._binding(term),
                            "lines": result.lines,
                            "outcome": result.outcome,
                            "rejections": result.rejections,
                            "published": False,
                        }
                        self.store.write_artifact(key, record)
                        if result.rejections:
                            self.store.log_event(
                                "glossary_note_not_adopted",
                                chapter=chapter,
                                start_index=start,
                                checkpoint=key,
                                rejections=result.rejections,
                            )
                        if result.outcome != "not_adopted":
                            self._publish(key, term.source, record)
                finally:
                    for future in futures:
                        future.cancel()
                if failed is not None:
                    raise failed
        self.store.write_artifact(f"{root}/done.json", {"terms": sorted(covered)})
        return covered

    @staticmethod
    def _binding(term: GlossaryTerm) -> str:
        return content_hash(
            [term.source, term.target, term.type, term.status, term.aliases, term.note]
        )

    def _publish(self, key: str, source: str, record: dict) -> None:
        with self.store.state_lock():
            current = self.store.get_term(source)
            # Compare before replay as well: an already-applied append or an editor's change
            # makes this proposal stale. Never overwrite a newer note, mapping or decision.
            if current is not None and record["before"] == self._binding(current):
                additions = [
                    line for line in record["lines"] if line not in current.note.splitlines()
                ]
                if additions:
                    note = "\n".join(([current.note] if current.note else []) + additions)
                    self.store.upsert_term(replace(current, note=note))
            self.store.write_artifact(key, {**record, "published": True})
