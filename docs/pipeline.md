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
-> Extract and update terminology as translation progresses
-> Optionally polish and normalize punctuation
-> Optionally run the experimental evidence-driven whole-book review
-> Optionally run whole-book consistency QA
-> Generate the report
-> Write translated content back and assemble the requested output
```

## Whole-book understanding and context

The prescan creates a digest for each chapter and a synopsis of the complete book. For every translation batch, the prompt presents stable information first: style guidance, the whole-book synopsis, the current chapter digest, relevant glossary terms, recent translated context, and finally the source text to translate.

This lets early chapters benefit from knowledge of later events while helping adjacent batches preserve pronouns, forms of address, tone, and sentences that span multiple source segments.

## Glossary

The initial analysis seeds the glossary. As translation proceeds, Wenyi extracts and updates people, places, organizations, terms, techniques, recurring expressions, and forms of address from completed source-and-target pairs. By default, later batches receive only terms that appear in the current chapter, keeping unrelated entries out of the prompt.

The glossary constrains later translation and supplies evidence to the final review, but it does not automatically rewrite every previously translated occurrence. Use `glossary list` and `glossary conflicts` to inspect entries, then combine review, QA, reports, and manual decisions when necessary.

## Quality controls

- **Segment alignment:** the model must return a JSON array with the same number of items as the input. Wenyi retries mismatched batches and falls back to translating one segment at a time.
- **Polishing:** improves Chinese fluency while preserving meaning and segment count.
- **Punctuation normalization:** converts punctuation to common Simplified Chinese full-width conventions.
- **Experimental Agent Review:** starts only after every chapter has been translated and uses the completed glossary. Contiguous chapter chunks are checked concurrently with the existing Reviewer prompt. Every response must end with a completion receipt containing the exact reviewed-segment count and `complete: true`. Syntax-only JSON damage is repaired locally with `json-repair`; a missing or invalid receipt recursively splits only the affected chunk, and a singleton receives at most `1 + review_output_retries` attempts.
- **Selective evidence loop:** when a successfully reviewed leaf chunk contains candidates and `review_agent_loop` is enabled, a bounded Agent Loop confirms, dismisses, or refines them and may add issues within that chunk. It can request one glossary entry by source or alias, the first, middle, last, or Nth occurrence of a term, nearby source-and-translation segments, and limited book, chapter, or style context instead of loading the whole book or glossary into every prompt. The loop uses the configured tier (`strong` by default) and must decide after at most `review_agent_max_evidence_rounds` evidence rounds.
- **Cross-chunk arbitration:** after all concurrent chunks finish, contradictory consistency proposals for the same term, pronoun, or fixed expression can be sent through a final arbiter. The Debug final-suggestion view conservatively rewrites every losing proposal to the winning value; every superseded proposal remains available for audit. It never changes the glossary or translated text.
- **Shadow Fix and blind re-review:** confirmed issues for the same segment are grouped into one Fixer request. The Fixer receives the style brief, book synopsis, chapter digest, relevant glossary subset, and nearby source/translation pairs, and must return one complete replacement segment rather than a diff. All Fixers in a round read one immutable shadow snapshot; their patches are applied together only after the round finishes. The next whole-book Review and evidence index read the updated shadow text without receiving the old issue explanations. Unresolved arbitration conflicts and unverified Agent fallbacks are left unresolved. The loop stops after consecutive clean passes, the configured Fix limit, no progress, or an A→B→A cycle.
- **Whole-book consistency QA:** checks terminology, references, voice, and punctuation after translation. It reports issues by default without rewriting the text.

Final review is disabled by default. Setting `pipeline.review: true` inserts it
between translation and QA in the one-command workflow. Review is also available
as an independent stage:

```bash
uv run trans-novel review book.epub
```

The explicit command runs even when `pipeline.review` is disabled. Every invocation
reviews the complete translated book from the beginning. It may update only a
Debug shadow translation and never changes chapter JSON, the manifest, glossary,
`report.json`, the formal event log, or the formal `usage.json`. Prompts, raw
responses, parsed actions, requested evidence, provisional patches, events,
remaining suggestions, and the run-local usage delta are written only to:

```text
state/<book>/debug/review-YYYYMMDD-HHMMSS-ffffff/
```

The debug directory contains its own `usage.json` with totals plus `by_tier` and
`by_stage` breakdowns. It is written for both successful and failed review runs.
`rounds/<NNN>/` contains each pass's overlay and Reviewer/Fixer traces; aggregate
files include `patches.json`, `not_rereported_patches.json`,
`unresolved_issues.json`, `fix_failures.json`, `rounds.json`,
`shadow_targets.json`, `summary.json`, and `result.json`.
`not_rereported_patches.json` means only that a subsequent blind review did not
report the logical issue covered by the patch again. It is an audit outcome, not
proof that the replacement is semantically correct. Stop reasons include
`clean_confirmed`, `max_rounds`, `no_progress`, `cycle_detected`, and
`unresolved_fixes` (a previously confirmed issue did not receive a valid patch
even if a later Reviewer missed it).

These debug traces contain source and translated passages. Treat them with the
same privacy and copyright care as the rest of the state directory.

## Resumability

Each completed translation batch is persisted immediately. Running `translate` again skips completed batches and fills only missing work. `assemble` can regenerate output directly from stored state.
