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
-> Extract and update terminology from accumulated evidence windows
-> Optionally polish and normalize punctuation
-> Run the final whole-book review against the completed glossary
-> Optionally run whole-book consistency QA
-> Generate the report
-> Write translated content back and assemble the requested output
```

## Whole-book understanding and context

The prescan creates a digest for each chapter and a synopsis of the complete book. For every translation batch, the prompt presents stable information first: style guidance, the whole-book synopsis, the current chapter digest, relevant glossary terms, recent translated context, and finally the source text to translate.

This lets early chapters benefit from knowledge of later events while helping adjacent batches preserve pronouns, forms of address, tone, and sentences that span multiple source segments.

## Glossary

The initial analysis seeds the glossary. As translation proceeds, Wenyi accumulates completed source-and-target pairs into evidence windows, then extracts and updates people, places, organizations, terms, techniques, recurring expressions, and forms of address. One larger terminology window can cover several smaller translation batches, reducing repeated model calls while giving the extractor more context. Set `pipeline.glossary_window_chars` lower for faster same-chapter feedback or higher for lower cost.

Each proposed term must be grounded on both sides: its source form must occur in the source window and its chosen Chinese form must occur in the corresponding translation. Proposed aliases that do not occur in the source window are discarded. Existing glossary context is also pruned to terms found in that window. Completed windows are checkpointed by a digest of their actual source and target text, so an unchanged resume makes no model call while edited translations invalidate only the affected window.

By default, later translation batches receive only terms that appear in the current chapter, keeping unrelated entries out of the prompt.

The glossary constrains later translation and the final review, but it does not automatically rewrite every previously translated occurrence. Use `glossary list` and `glossary conflicts` to inspect entries, then combine review, QA, reports, and manual decisions when necessary.

## Quality controls

- **Segment alignment:** the model must return a JSON array with the same number of items as the input. Wenyi retries mismatched batches and falls back to translating one segment at a time.
- **Polishing:** improves Chinese fluency while preserving meaning and segment count.
- **Punctuation normalization:** converts punctuation to common Simplified Chinese full-width conventions.
- **Final review:** starts only after every chapter has been translated, so each chapter derives its relevant term snapshot from the completed glossary rather than the glossary state from an earlier chapter. Chapters are divided into contiguous chunks and checked in parallel against fixed final translation and term snapshots; results are merged back in book order. Severe issues are only retranslated when `autofix_severe` is enabled.
- **Whole-book consistency QA:** checks terminology, references, voice, and punctuation after translation. It reports issues by default without rewriting the text.

Final review is disabled by default. Setting `pipeline.review: true` inserts it
between translation and QA in the one-command workflow. Review is also available
as an independent, resumable stage:

```bash
uv run trans-novel review book.epub
uv run trans-novel review book.epub --force
uv run trans-novel review book.epub --fix     # --no-fix overrides automatic fixes
```

The explicit command runs even when `pipeline.review` is disabled. `--force`
rechecks chapters whose current translations have already been reviewed;
`--fix` and `--no-fix` override `pipeline.autofix_severe` for that invocation.

## Resumability

Each completed translation batch is persisted immediately. Running `translate` again skips completed batches and fills only missing work. `assemble` can regenerate output directly from stored state.
