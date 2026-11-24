# Translation workflow

Wenyi prepares a book before translating its chapters, saves completed work as it
goes, then reviews and exports the translation. This guide explains the choices that
affect quality, time, cost, and recovery. For commands, use the [CLI guide](cli.md);
for settings, use the [configuration reference](../reference/configuration.md).
Input preservation and export limitations belong to the
[format reference](../reference/formats.md).

<a id="overview"></a>

## The default workflow

1. Parse and save the source.
2. Prepare chapter digests, style guidance and a whole-book synopsis.
3. Translate and polish chapters, align formatting and update the glossary.
4. Review the complete book and publish revisions when Autofix is enabled.
5. Export the saved translation.

The defaults are standard translation, whole-book understanding, polishing,
annotation alignment, final Review, and **Autofix publishing enabled**.
Three-draft precision and incremental glossary notes are opt-in.
The default export is monolingual; a bilingual edition is optional.

<a id="quality-and-cost"></a>

### Quality and cost choices

| Choice | What it buys | Cost or limitation |
| --- | --- | --- |
| Whole-book understanding | Chapter facts and an overview before the first body batch | Reads all chapter source text; long chapters need multiple prescan calls |
| Polishing | A second pass for target-language expression | Adds a model call to a normal standard batch; fluency is not proof of fidelity |
| Three-draft precision | Three independent attempts followed by source-aware synthesis | Normally four body-stage calls instead of two with standard translation and polishing |
| Annotation/style alignment | Places links and mixed formatting in translated text | Partial spans can require additional model calls; uncertain placements use format-specific fallbacks |
| Review and Autofix | Whole-book consistency checks, evidence gathering and revisions | Can make several passes and additional Fixer calls; publishing changes the saved translation |
| Larger context or terminology evidence | More cross-passage evidence | Larger prompts and, for evidence production, additional calls |

Disabling a stage reduces its work, not necessarily the total cost in a predictable
ratio. Retries, output length, reasoning tokens, and provider prices matter. Prompt
prefix reuse may help provider caching but does not guarantee a cache hit.
None of these options guarantees a more accurate translation; compare representative
passages and review important results yourself.

<a id="reasoning-effort"></a>

### Reasoning effort by stage

On reasoning models, hidden thinking tokens often account for the largest share of
output spend and wall-clock latency. You can override `thinking` per operation in
`llm.routes` (see [connections, profiles, and routes](../reference/configuration.md#model-profiles)):

| Operation | Recommended setting | Trade-off |
| --- | --- | --- |
| `translation.body` | `thinking: "off"` under `nextgen` (`polish_mode: sparse`); keep reasoning on without sparse polishing | In controlled A/B runs on DeepSeek V4.1 Flash under `nextgen`, turning reasoning off cut body completion tokens by ~71% while blind judges rated the final polished prose near parity with `high` (`8.35` vs `8.40` / 10). |
| `glossary.align_history` | `thinking: "off"` | Small alignment calls; disabling reasoning cut completion tokens by ~85% and ran ~3× faster with exact match parity. |
| `glossary.extract` | `thinking: low` (do **not** disable) | Enough to extract names reliably while much faster than `high`. Disabling reasoning risks wrong mappings for forms of address in `incremental` mode, and in `compiled` (Book Bible) mode lets common-word/substring noise into the glossary, which then triggers false `glossary_miss` alerts and triples escalated polish calls. |
| `polish.body` | `thinking: low` under `polish_mode: sparse` | Keeps routine batches cheap while allowing risk-routed `thinking_boost` to escalate batches that trigger a high-severity alert or meet both the classical-dense and dialogue-density thresholds. |

Quote `"off"` in YAML so it is parsed as a string rather than boolean `false`, and enable sparse polishing when disabling reasoning on `translation.body`:

```yaml
pipeline:
  polish: true
  polish_mode: sparse

llm:
  routes:
    translation.body: {model: deepseek/deepseek-flash, thinking: "off"}
    polish.body: {model: deepseek/deepseek-flash, thinking: low}
    glossary.extract: {model: deepseek/deepseek-flash, thinking: low}
    glossary.align_history: {model: deepseek/deepseek-flash, thinking: "off"}
```

Exact savings and quality vary by model family and language pair; spot-check a few
chapters before applying lower effort to a full book.

<a id="preparation"></a>

## Preparation before translation

Parsing identifies chapters and paragraphs without calling a translation model.
`wenyi parse` and Web/Desktop upload parsing save a read-only
[source snapshot](../reference/formats.md#source-snapshots). PDF parsing can still
require MinerU or BabelDOC. Parsing alone does not initialize translation state.

`prepare`, or preparation at the start of `translate`, then:

1. Detects the source language when `language.source` is `auto`. An explicit source
   language skips that request.
2. With `pipeline.book_understanding: true`, summarizes the **complete source of
   every chapter**, splitting long chapters into bounded chunks.
3. Builds style and character-voice guidance from original opening, middle and
   ending samples, with chapter digests as factual context.
4. Builds the whole-book synopsis from that guidance and the chapter digests.

Preparation **does not translate body text or extract glossary entries**. It keeps
existing project terms, including imported mappings. Turning off book understanding
skips chapter digests and the synopsis, not style analysis.

With book understanding enabled, a chapter containing text must have a usable digest
before body translation starts. Digest failure stops preparation; a failed whole-book
synopsis produces a warning and allows translation to use the chapter digests instead.
Complete successful digests and style analysis are reusable on a matching retry.
An unfinished chapter's individual prescan chunks are not separately checkpointed.

<a id="context-and-glossary"></a>

## Translation context and the glossary

Chapters and body batches progress in source order. A normal batch receives style
guidance, the book synopsis, the **current glossary** (full or batch-relevant, see
below), its chapter digest, relevant source notes, recent translated text, the requested source, and one following
source segment as a read-only reference. The following segment helps with continuity;
it is not translated early or included in the expected output count.

Recent translation is reconstructed from saved targets, including preceding chapters.
`pipeline.rolling_context_tokens` defaults to 512 tokens.
It favors complete recent paragraphs; an oversized newest
paragraph contributes only its tail. Setting the budget to zero removes preceding
translation, not the following source reference. BabelDOC uses a
[layout-aware context boundary](../reference/formats.md#babeldoc) instead.

<a id="deferred-polishing"></a>

With `pipeline.polish_concurrency` above 1 in standard mode, polishing no longer
blocks translation. Each draft is saved and queued for polishing, and the next batch
starts at once. Recent-translation context and glossary extraction always read the
unpolished drafts, so results do not depend on when polishing finishes. This trades a
little continuity for throughput with a slow polisher. A chapter is marked done only after all its polish
jobs are applied. Annotation alignment runs on the polished text. If polishing fails
or returns the wrong paragraph count, the draft stays as the translation and a
`batch_polish_failed` event is recorded. An interrupted run repolishes the saved
drafts on resume and does not translate them again.

Ordinary glossary extraction (`pipeline.glossary_mode: incremental`) starts with completed
source-and-translation pairs. Without imported terms, the first batch therefore has no
predefined mappings. New terms and conflicts inform later batches; they do not automatically
rewrite earlier translations. Each batch's extraction runs while the next batch translates, so
a new entry reaches the batch after that; the next batch still sees the previous batch's
wording in its recent-translation context. A chapter is marked done only after its
terms are stored. The glossary is authoritative for name mappings, while style
guidance retains source identities and descriptive character information.

With `pipeline.glossary_mode: compiled`, Wenyi instead mines recurring entities from chapter
digests and the source text, probes and adjudicates a frozen Book Bible before chapter 1, and
skips per-batch and chapter-end extraction completely (eliminating the one-batch lag and
unlocking `pipeline.chapter_concurrency` above 1). User-imported and manually edited entries
are preserved unchanged.

By default (`pipeline.batch_glossary: all`) each pending body batch receives all
current entity mappings, while newly stored EPUB navigation titles (`navigation`) are
included only when they occur in that request's text (legacy projects with older
`term`-typed navigation rows get this scoping automatically in Review). With `batch_glossary: relevant`, each batch instead receives only
entries whose source or aliases occur in its source paragraphs, the following-source
reference, its annotation contexts or the chapter digest, and glossary extraction
sees only existing entries that occur in the extracted text. A long book's full
glossary can grow to tens of thousands of tokens per request, so `relevant` is the
cheaper choice for long books. `pipeline.glossary_prompt_tokens` is an optional cap
and is off (`0`) by default. When set, `all` keeps its append-stable leading entries
within the cap (preserving prefix caches) plus the later entries matching that batch,
and `relevant` sends only the matching entries that fit. Before sending a request, Wenyi also checks
estimated prompt tokens plus output reserve against the model's `context_window`,
shrinking the glossary or raising an actionable error rather than overflowing the
provider limit. A paragraph that names a
character only by pronoun or title then relies on the digest and recent translations
for that mapping. Fresh Reviewer requests receive the final entity glossary plus any
navigation titles occurring in the reviewed chunk, fitted to the review model's
`context_window` (and split automatically if a multi-paragraph chunk still overflows).
Evidence queries and individual Fixers can use relevant subsets. Each
`batch_translated` event records the entries (`glossary_terms`) it sent and an
estimate of the glossary block's `cl100k_base` tokens (`glossary_tokens`).

Extraction therefore keeps the glossary selective: names, book-specific terms,
recurring forms of address and fixed expressions, not everyday vocabulary, one-off
descriptions or single lines. Wenyi also enforces this without the model: a new entry
whose source occurs only once in the whole book is not stored, and for Chinese sources
self-references and forms of address whose wording depends on speaker and listener
(such as 孤, 本王, 大人 or 公子) and one-character common words are not stored either;
one-character names such as 蜀 are kept. An extracted translation that differs from the
stored one only in case, spacing, a leading "the" or a final stop is not recorded as a
conflict; glossary imports and manual edits still compare exactly.

Two further checks need no word list for the source language. When translating into
English, an extracted entry whose translation is only pronouns and title words ("I",
"your son", "Your Excellency") is not stored, while "Lord Jiang" keeps its name and
stays. During translation, an extracted entry that the finished translation keeps
overriding is left out of later translation and polish prompts. Counting starts at the
earliest draft paragraph, in book order, that uses its stored translation, so paragraphs
earlier in the book than a corrected translation's first use do not count against it; once its source has appeared in
at least five such paragraphs and fewer than 35% use the translation, it is left out.
The entry stays in the glossary. Entries you import, add, edit or resolve are always
sent. A `glossary_terms_left_out` event records each change. Translations are matched
as written, so in a target language that inflects names (Russian "Лондоне" for
"Лондон") an entry the translator follows can still be left out.

Extraction runs after every batch and once per chapter. The DeepSeek preset runs
`glossary.extract` at `thinking: low`, which is enough to read names off a finished
translation and takes far less time than high effort. For per-stage reasoning
recommendations—and why `glossary.extract` should stay at `low` rather than `"off"`—see
[Reasoning effort by stage](#reasoning-effort).

After EPUB body translation, Wenyi completes the parsed navigation tree and adds its
source-and-translation pairs to the glossary before final Review, including nested,
reused and previously saved titles. Existing glossary translations are retained;
different navigation translations become conflicts for resolution. Review uses these
mappings to check body headings, visible contents and prose. Publishing its revisions
requires Autofix. With Review disabled, navigation terms are still saved, but this
step does not synchronize body text or visible contents during export.

No person discovery or whole-book terminology verification runs before translation. Terms are extracted after translation; optional [incremental CLI notes](cli.md#terminology-context) maintain cited facts.

## Three-draft precision

`pipeline.translation_mode: three_draft_synthesis` means **three independent drafts followed
by one source-aware synthesis**, not choosing a winning draft.
Polishing must be enabled.

```text
Same frozen source and context
  ├─ independent draft T1 ─┐
  ├─ independent draft T2 ─┼─ source-aware synthesis → final translation
  └─ independent draft T3 ─┘
```

The three drafts run concurrently and do not see one another. Synthesis receives
their proposals as fallible data, prioritizes the source, and combines useful parts.
There is no independent accuracy judge. The stored before-polish comparison is T1,
not a draft certified to be the most accurate.

A normal batch makes three draft calls and one synthesis call,
versus one translation and one polish call in standard mode. It uses the same model
routes and output limits. Four versus two calls does **not** mean a fixed 2× price:
input sizes, output tokens, retries and the other book stages still count.

If a draft or synthesis cannot produce a usable result after the configured retries,
the batch stops instead of silently switching to single-paragraph translation.
Only the failed stage is retried; compatible successful drafts remain reusable on resume.

Use the same source, mode, context and model settings to reuse pending checkpoints.
Changing the mode does not redo completed formal translations. Commands and
creation-time project choices are documented in the
[CLI guide](cli.md) and [configuration reference](../reference/configuration.md).

## Review and Autofix

Review requires every chapter to be translated. It examines contiguous chunks with
the final glossary, can request evidence for uncertain issues, reconciles conflicting
recommendations, and may propose revisions. Additional rounds recheck the revised
text and add model usage.

**`pipeline.review_autofix` defaults to `true`, so Review can change the saved
translation.** With Autofix disabled, new Review work produces recommendations
without publishing them to the book.

To request recommendations without starting publication:

```bash
wenyi review book.epub --no-autofix
```

Use `pipeline.review_autofix: false` for the same preference in the complete workflow.
`--autofix` forces publishing for an independent Review when that preference is off.
Disabling Autofix prevents new publication, but an already-started, journaled
publication is recovered first and can still write formal targets even when this
invocation uses `--no-autofix`. The switch is not a rollback or a way to cancel
that committed publication.

`pipeline.review: false` or `translate --no-review` skips the automatic Review stage.
An explicit `wenyi review` still runs. Matching content, glossary, guidance, language
policy and model identities reuse a completed result or resume unfinished work;
**each invocation does not necessarily create a new Review**. Changes to those
inputs can require a fresh run and new model calls.

Review may stop at its round budget, with unresolved issues, or after detecting no
progress or a revision cycle. A later pass not reporting an issue again is not proof
that its replacement is correct. Inspect the saved result before relying on a
publication for accuracy-sensitive work.

## Pause and resume

Rerun the same input with the same target language and compatible configuration.
CLI state is isolated under `state/<book>/targets/<target-language>/`; Web and
Desktop keep independent project state. A different file with the same name is not
the same source.

- Completed translation batches remain saved. Changing polishing or translation mode
  does not automatically reprocess them.
- Resume may first finish terminology extraction for an already translated batch;
  this does not mean the batch is being translated again.
- Preparation, precision and Review reuse successful matching checkpoints rather than
  requesting their completed model stages again. In-flight work without a durable
  success record may need another request.
- A language-policy or prompt change can rebuild affected derived guidance and incur
  model usage while preserving completed translations and glossary entries.
- Finishing translation can retry previously failed annotation placements. A
  `translate` invocation on a completed book is therefore not necessarily model-free.

Use `wenyi status` for saved progress and `wenyi inspect-run` for recorded events,
usage and timing. Cumulative token totals include earlier attempts and resumes, not
only the latest invocation. Keep state and source files when recovering; use a
separate project/state directory for intentional fresh translations or comparisons.

<a id="re-export"></a>

## Re-export without translating again

`wenyi assemble` renders saved state **without translation-model calls**. Use it to change
the output format, bilingual order, or source styling.
It does not rerun preparation, polishing, Review, or annotation repair.

An export uses one consistent snapshot of the saved book.
Translation may continue elsewhere; export again to include batches saved afterward.
Original templates and, for PDF, the BabelDOC session may still be required.
See [export snapshots](../reference/formats.md#export-snapshots) and
[PDF requirements](../reference/formats.md#pdf).

For checkpoint ownership, publication journals, cache identities and accounting
rules, see the [developer workflow contracts](../development/pipeline.md).
