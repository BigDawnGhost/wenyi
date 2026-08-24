# Translation pipeline

[简体中文](zh/pipeline.md)

Wenyi first builds a whole-book understanding and then translates chapters in order. Optional stages can be disabled in `config.yaml` to reduce cost or runtime.

```text
Read input
-> Parse chapters, text segments, and the EPUB table of contents
-> Detect the source language or use the configured language
-> Scan the book and create chapter digests and a whole-book synopsis
-> Analyze representative passages and build an initial glossary and style guide
-> Translate chapter by chapter and batch by batch
-> Optionally polish each completed batch
-> Immediately align each annotated EPUB logical paragraph in sequence
-> Extract and update terminology as translation progresses
-> Normalize remaining punctuation at the end of each chapter
-> Optionally run the evidence-driven whole-book review
-> Generate the report
-> Write translated content back and assemble the requested output
```

## Whole-book understanding and context

The prescan creates a digest for each chapter and a synopsis of the complete book. For every translation batch, the prompt presents stable information first: style guidance, the whole-book synopsis, the current chapter digest, relevant glossary terms, any source-language notes referenced by the current segments, recent translated context, and finally the source text to translate. Recent translation therefore remains immediately adjacent to the new source passage.

This lets early chapters benefit from knowledge of later events while helping adjacent batches preserve pronouns, forms of address, tone, and sentences that span multiple source segments.

## Glossary

The initial analysis seeds the glossary. As translation proceeds, Wenyi extracts and updates people, places, organizations, terms, techniques, recurring expressions, and forms of address from completed source-and-target pairs. By default, later batches receive only terms that appear in the current chapter, keeping unrelated entries out of the prompt.

The glossary constrains later translation and supplies evidence to the final review, but it does not automatically rewrite every previously translated occurrence. Use `glossary list` and `glossary conflicts` to inspect entries, then combine Review results, reports, and manual decisions when necessary.

## Quality controls

- **Segment alignment:** the model must return a JSON array with the same number of items as the input. Wenyi retries mismatched batches and falls back to translating one segment at a time.
- **Polishing:** improves Chinese fluency while preserving meaning and segment count.
- **Punctuation normalization:** converts punctuation to common Simplified Chinese full-width conventions.
- **EPUB annotation context:** during preparation, Wenyi resolves high-confidence footnote and endnote references to their source-language note bodies, deduplicates shared targets, and stores an auxiliary copy separately from chapter text. Translation batches automatically receive that copy only for the numbered segments that reference it. Backlinks, chapter jumps, external links, and other ordinary hyperlinks are excluded. The borrowed copy is never appended to the referencing segment or rolling context; note resources already present in the EPUB spine remain ordinary translatable book content.
- **EPUB annotation alignment:** removes recognized footnote markers from translatable source text while retaining semantic superscripts/subscripts. As soon as an annotated logical paragraph has been fully translated, polished, and punctuation-finalized, Wenyi makes one sequential alignment call and immediately persists the restored `a/sup/href/id/class` positions. Split continuations are rejoined first; unrelated paragraphs make no call. The target text is immutable during alignment, and failures degrade to clickable end markers instead of dropping links. Untranslated text and bilingual source copies keep the source EPUB's original annotation positions. EPUB state created before this metadata format must be prepared again from the source book.
- **Agent Review:** starts only after every chapter has been translated and uses the completed glossary. Contiguous chapter chunks are checked concurrently with the existing Reviewer prompt. Every response must end with a completion receipt containing the exact reviewed-segment count and `complete: true`. Syntax-only JSON damage is repaired locally with `json-repair`; a missing or invalid receipt recursively splits only the affected chunk, and a singleton receives at most `1 + review_output_retries` attempts.
- **Selective evidence loop:** when a successfully reviewed leaf chunk contains candidates and `review_agent_loop` is enabled, a bounded Agent Loop confirms, dismisses, or refines them and may add issues within that chunk. It can request one glossary entry by source or alias, the first, middle, last, or Nth occurrence of a term, nearby source-and-translation segments, and limited book, chapter, or style context instead of loading the whole book or glossary into every prompt. The loop uses the configured tier (`strong` by default) and must decide after at most `review_agent_max_evidence_rounds` evidence rounds.
- **Cross-chunk arbitration:** after all concurrent chunks finish, contradictory consistency proposals for the same term, pronoun, or fixed expression can be sent through a final arbiter. The final suggestion set conservatively rewrites every losing proposal to the winning value; every superseded proposal remains available in the round traces. It never changes the glossary or translated text.
- **Shadow Fix and blind re-review:** confirmed issues for the same segment are grouped into one Fixer request. The Fixer receives the style brief, book synopsis, chapter digest, relevant glossary subset, and nearby source/translation pairs, and must return one complete replacement segment rather than a diff. All Fixers in a round read one immutable shadow snapshot; their patches are applied together only after the round finishes. The next whole-book Review and evidence index read the updated shadow text without receiving the old issue explanations. Unresolved arbitration conflicts and unverified Agent fallbacks are left unresolved. The loop stops after consecutive clean passes, the configured Fix limit, no progress, or an A→B→A cycle.
Final review is the sole model-driven semantic review stage and is disabled by
default. Setting `pipeline.review: true` runs it after translation in the
one-command workflow. Review is also available as an independent stage:

```bash
uv run trans-novel review book.epub
```

The explicit command runs even when `pipeline.review` is disabled. Every invocation
reviews the complete translated book from the beginning. It may update only a
run-local shadow translation and never changes chapter JSON, the manifest, or the
glossary. The final result, run-local usage delta, events, and internal round traces
are written to:

```text
state/<book>/reviews/review-YYYYMMDD-HHMMSS-ffffff/
```

The directory has only `result.json`, `usage.json`, `events.jsonl`, and `rounds/`.
`result.json` contains the final issues and folded modification suggestions;
chapter and segment indices point back to the formal chapter JSON instead of
copying source text and context. `rounds/` retains prompts, responses, patches,
and failures for diagnosis. The run-local usage delta is also merged exactly once
into the book's cumulative `usage.json`, while `report.json` receives only the
Review ID, stop reason, counts, and `read_only: true`.

`not_rereported` means only that a subsequent blind review did not report the
logical issue covered by the suggestion again. It is not proof that the proposed
replacement is semantically correct. Stop reasons include
`clean_confirmed`, `max_rounds`, `no_progress`, `cycle_detected`, and
`unresolved_fixes` (a previously confirmed issue did not receive a valid patch
even if a later Reviewer missed it).

## Resumability

Each completed translation batch is persisted immediately. Running `translate` again skips completed batches and fills only missing work. A standalone `assemble` briefly freezes the persisted manifest and chapter snapshot, releases the state lock, and renders from that snapshot, so it does not wait for a full translation running in another terminal.

## Subtitle path (SRT)

`.srt` files take a parallel light path under `trans_novel.srt`, not the book
Orchestrator above. There is no whole-book prescan, glossary, polishing, or
Review. Translation uses overlapping cue windows with high concurrency on the
strong model tier; progress is stored under `state/srt/<slug>/` with
`cues.jsonl`, batch caches, `usage.json`, and `events.jsonl`. See
[Usage guide — SRT subtitles](usage.md#srt-subtitles).
