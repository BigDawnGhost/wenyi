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
-> Optionally run the evidence-driven whole-book review
-> Optionally publish Review Autofix revisions to formal segment targets
-> Generate the report
-> Optionally normalize punctuation on an export-only copy
-> Write the copy back and assemble the requested output
```

## Language rules and state scope

Source and target are independent choices. Body translation, titles, term renderings and notes, analysis descriptions, polishing, chapter digests, and book synopses are requested in the target language. Character references in prose use target-language names; `source` and `aliases` retain their original spelling. Task instructions use English and live in `trans_novel/i18n/data/tasks/`; source understanding, target expression, pair-specific honorific rules, and metadata language constraints live alongside them in `languages/`, `pairs/`, and `shared/`. JSON keys and stable identities remain unchanged. Glossary type/gender values use English identifiers; older Chinese enum values are no longer converted. Analysis also accepts a model's list of style-guide bullets without discarding it. Existing analysis and notes remain intact on resume; resource updates apply to new model calls.

All targets, including `zh`, own separate state under `state/<book>/targets/<target-language>/`. Completed segments still skip model calls; updated resources affect subsequent requests. Initialization records a prompt fingerprint, run events record applied resources, and Review cache identity includes languages, honorific strategy, and the resource fingerprint. Manifest-last initialization, atomic writes, domain locks, and Review/Autofix publication boundaries remain in place. See [P10](project-review/2026-09-05/p10-multilingual-internationalization.md).

## Whole-book understanding and context

The prescan creates a digest for each chapter and a synopsis of the complete book. For every translation batch, the prompt presents stable information first: style guidance, the whole-book synopsis, the current chapter digest, relevant glossary terms, any source-language notes referenced by the current segments, recent translated context, the source text to translate, and one following source segment. Recent translation therefore remains immediately adjacent to the new source passage.

This lets early chapters benefit from knowledge of later events while helping adjacent batches preserve pronouns, forms of address, tone, and sentences that span multiple source segments.

The following segment is a quoted, read-only reference from the same chapter. It helps the translator recognize a sentence or dialogue that continues beyond the batch, including fragments split from a long paragraph, and avoid inventing an ending or forcing final punctuation. The reference is excluded from the numbered inputs and output count; its content must not be translated early or borrowed to complete the current paragraph. It is also supplied to polishing. At a chapter end there is no following reference; the workflow does not cross into the next chapter. This is built in and remains enabled when `rolling_context_segments` is zero, which disables only preceding translations.

Alignment retries retain the reference. Single-paragraph fallback uses that paragraph's immediate source neighbor, including an unchanged number or symbol. Resume recomputes the neighbor from source order after splitting completed and pending batches, preserving completed targets and stable segment identities. Lookahead is never added to the saved rolling translation context. It adds at most one source segment to each translation or polishing request, with no extra model call. This supplies continuity evidence; actual wording and sentence endings still depend on the model.

## Glossary

The initial analysis seeds the glossary. As translation proceeds, Wenyi extracts and updates people, places, organizations, terms, techniques, recurring expressions, and forms of address from completed source-and-target pairs. By default, later batches receive only terms that appear in the current chapter, keeping unrelated entries out of the prompt.

The glossary constrains later translation and supplies evidence to the final review, but it does not automatically rewrite every previously translated occurrence. Use `glossary list` and `glossary conflicts` to inspect entries, then combine Review results, reports, and manual decisions when necessary.

## Quality controls

- **Segment alignment:** the model must return a JSON array with the same number of items as the input. Wenyi retries mismatched batches and falls back to translating one segment at a time.
- **Polishing:** improves target-language fluency while preserving meaning and segment count.
- **Punctuation normalization:** optionally converts punctuation to common Simplified Chinese full-width conventions on an export-only copy for Simplified Chinese targets; other targets skip this conversion. It never rewrites formal chapter `target` values, so changing this output option does not alter translation, Review, or resume state.
- **EPUB annotation context:** during preparation, Wenyi resolves high-confidence footnote and endnote references to their source-language note bodies, deduplicates shared targets, and stores an auxiliary copy separately from chapter text. Translation batches automatically receive that copy only for the numbered segments that reference it. Backlinks, chapter jumps, external links, and other ordinary hyperlinks are excluded. The borrowed copy is never appended to the referencing segment or rolling context; note resources already present in the EPUB spine remain ordinary translatable book content.
- **EPUB annotation alignment:** removes recognized footnote markers from translatable source text while retaining semantic superscripts/subscripts. As soon as an annotated logical paragraph has been fully translated and polished, Wenyi makes one sequential alignment call against the formal target and immediately persists the restored `a/sup/href/id/class` positions. When export punctuation normalization is enabled, the export layer remaps those offsets together with the normalized in-memory copy. Split continuations are rejoined first; unrelated paragraphs make no call. Failures degrade to clickable end markers instead of dropping links. Untranslated text and bilingual source copies keep the source EPUB's original annotation positions. EPUB state created before this metadata format must be prepared again from the source book.
- **Agent Review:** starts only after every chapter has been translated and uses the completed glossary. Contiguous chapter chunks are checked concurrently with the existing Reviewer prompt. Every response must end with a completion receipt containing the exact reviewed-segment count and `complete: true`. Syntax-only JSON damage is repaired locally with `json-repair`; a missing or invalid receipt recursively splits only the affected chunk, and a singleton receives at most `1 + review_output_retries` attempts.
- **Selective evidence loop:** when a successfully reviewed leaf chunk contains candidates and `review_agent_loop` is enabled, a bounded Agent Loop confirms, dismisses, or refines them and may add issues within that chunk. It can request one glossary entry by source or alias, the first, middle, last, or Nth occurrence of a term, nearby source-and-translation segments, and limited book, chapter, or style context instead of loading the whole book or glossary into every prompt. The loop uses the configured tier (`strong` by default) and must decide after at most `review_agent_max_evidence_rounds` evidence rounds.
- **Cross-chunk arbitration:** after all concurrent chunks finish, contradictory consistency proposals for the same term, pronoun, or fixed expression can be sent through a final arbiter. The final suggestion set conservatively rewrites every losing proposal to the winning value; every superseded proposal remains available in the round traces. It never changes the glossary or translated text.
- **Shadow Fix and blind re-review:** confirmed issues for the same segment are grouped into one Fixer request. The Fixer receives the style brief, book synopsis, chapter digest, relevant glossary subset, and nearby source/translation pairs, and must return one complete replacement segment rather than a diff. All Fixers in a round read one immutable shadow snapshot; their patches are applied together only after the round finishes. The next whole-book Review and evidence index read the updated shadow text without receiving the old issue explanations. Unresolved arbitration conflicts and unverified Agent fallbacks are left unresolved. The loop stops after consecutive clean passes, the configured Fix limit, no progress, or an A→B→A cycle.
- **Optional Autofix publishing:** the Review engine itself remains read-only. When `review_autofix` is enabled, a separate publisher first overlays the folded `changes`, then sends final unresolved issues through the existing Review Agent Loop against that updated translation. Confirmed issues reuse the existing Fixer; no Autofix-specific loop or prompt exists. The publisher writes only final complete segments to formal `target` values, then refreshes annotation and DOCX style offsets.
Final review is the sole model-driven semantic review stage and is enabled by
default. Setting `pipeline.review: false` or passing `--no-review` skips it in the
one-command workflow. Review is also available as an independent stage:

```bash
uv run trans-novel review book.epub
uv run trans-novel review book.epub --autofix
```

The explicit command runs even when `pipeline.review` is disabled. Matching completed
results are reused; an interrupted Review resumes its saved rounds, chunks, and agent
traces when content, configuration, and glossary fingerprints match. Recoverable stops
such as Ctrl+C, timeouts, transport failures, HTTP 429/5xx, and provider balance/quota
errors (for example HTTP 402) leave the run as `interrupted` so the next `review`
command can continue instead of starting a new directory. Permanent local failures still
finish as `failed`. Otherwise, a new whole-book Review starts. Cached chunks and
completed initial screening skip chapter glossary matching; pending reviewer requests
share one chapter-wide glossary snapshot.
The CLI shows chapter loading and checkpoint preparation before reviewing paragraphs.
Elapsed time measures the entire current workflow and never resets at stage or round
boundaries. It continues advancing while model requests are pending, even after a stage
reaches its final count. Each invocation's duration is saved in the target's `timing.json`
and accumulated across resumes, excluding downtime. Paragraph counts advance when a top-level chunk
finishes, including chunks restored from cache.

The Review engine first updates a run-local shadow translation. Publishing is enabled by default;
set `pipeline.review_autofix: false` or pass `--no-autofix` to keep Review from
replacing formal chapter `target` values. The manifest and glossary are never changed.
The final result, run-local usage delta, events, and internal traces are written to:

```text
state/<book>/targets/<target-language>/reviews/review-YYYYMMDD-HHMMSS-ffffff/
```

The base Review directory contains `result.json`, `usage.json`, `events.jsonl`, and
`rounds/`.
`result.json` contains the final issues and folded modification suggestions;
chapter and segment indices point back to the formal chapter JSON instead of
copying source text and context. `rounds/` retains prompts, responses, patches,
and failures for diagnosis. Autofix adds `autofix/index.json`, which keeps each
before/after chain, issue ID, Agent decision, failure, target hash, and publication
status. Chapter JSON receives no additional Review history field. The journal is
written before formal targets and allows an interrupted publication to resume
idempotently. A partial final issue fix does not roll back a valid direct `change`;
the index and result summary report the failure. The run-local usage delta is also
merged exactly once into the book's cumulative `usage.json`, while `report.json`
receives a compact Review/Autofix summary and sets `read_only: false` for a published
run.

`not_rereported` means only that a subsequent blind review did not report the
logical issue covered by the suggestion again. It is not proof that the proposed
replacement is semantically correct. Stop reasons include
`clean_confirmed`, `max_rounds`, `no_progress`, `cycle_detected`, and
`unresolved_fixes` (a previously confirmed issue did not receive a valid patch
even if a later Reviewer missed it).

## Resumability

Each completed translation batch is persisted immediately. When polishing is enabled, each segment in the chapter JSON keeps the translation-stage text in `target_before_polish` and the polished final text in `target`. Running `translate` again skips completed batches and fills only missing work. A standalone `assemble` briefly freezes the persisted manifest and chapter snapshot, releases the state lock, and renders from that snapshot, so it does not wait for a full translation running in another terminal.

## Subtitle path (SRT)

`.srt` files take a parallel light path under `trans_novel.srt`, not the book
Orchestrator above. There is no whole-book prescan, glossary, polishing, or
Review. Translation uses overlapping cue windows with high concurrency on the
strong model tier; progress is stored under `state/srt/<slug>/targets/<target-language>/` with
`cues.jsonl`, batch caches, `usage.json`, and `events.jsonl`. See
[Usage guide — SRT subtitles](usage.md#srt-subtitles).

## Model registration and usage

All model calls use stable operation IDs from `llm/operations.py`; `llm/registry.py` registers provider adapters. Runtime and the separate SRT workflow each own one routed client, reusing SDK connections and sharing invocation concurrency, quotas and usage. Agents select no provider or tier; Orchestrator retains only assembly and workflow routing.

To add a model operation, register an `OperationSpec` with its ID, default tier or inherited operation, output hint, workflow flags and protocol version, then call `complete(..., operation="domain.operation")` in the domain service. Validation, CLI previews and inference fingerprints read the same registry. To add a provider, implement its options, request builder, usage normalization and `ProviderAdapter` under `llm/providers/`, then register a `ProviderSpec`. Keep SDK initialization lazy and SDK retries disabled. Change the relevant protocol version when request semantics change, and cover requests, usage and resume behavior with offline tests. Registries are immutable after startup.

Review compares the effective inference identity of reachable operations. Model, endpoint or option changes create a new Review, while unrelated routes or concurrency changes preserve caches. Pending Autofix publication takes priority; evidence traces are never replayed under another model. Book and Review ledgers journal their snapshots in `usage-pending.json` before updating each `usage.json`, allowing idempotent recovery.
