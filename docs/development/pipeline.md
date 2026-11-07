# Workflow contracts

This page defines persistent workflow behavior for maintainers. It is not a release
history or a catalogue of every internal record. User-facing stages and tradeoffs
belong to the [workflow guide](../guide/pipeline.md); preservation rules belong to
the [format reference](../reference/formats.md). See [architecture](architecture.md)
for package ownership and dependency boundaries.

<a id="ownership"></a>

## Ownership and execution

The Orchestrator is a thin façade over preparation, translation, annotation, Review,
Autofix and finalization services. Runtime supplies configuration, frozen language
plans, storage and one routed model client. Domain services own their concurrency
and checkpoints; agents do not own workflow state or select providers.

Keep concurrent results in stable input order. Completion order must not change
chapter content, draft identity, annotations, glossary ordering or Review results.
Use the operation registry for model routing, validation and inference identities.
Provider transport retries belong to the shared retry layer; disable SDK retries
instead of stacking retry loops around it.

<a id="initialization"></a>

## Initialization and source identity

Parsing and initialized translation are distinct states.

- `parsed_document.json` binds the source hash and parsing configuration to a
  source-only document. It can support read-only preview before AI preparation,
  including after preparation fails. It does not authorize editing staged targets
  or imply that a manifest has committed.
- Initialization establishes source identity, stages chapters and language-policy
  artifacts, generates required chapter digests and style analysis, and **commits
  the initialized manifest last**. Synopsis generation follows initialization;
  a usable synopsis is not an initialization-success requirement.
- A manifest binds `source_sha256`, source language and target language.
  Different content cannot resume by sharing a filename or title. Hash checks around
  parsing and export detect source replacement during those operations.
- Retry preserves compatible parsed source, complete chapter digests, paid style
  analysis and cumulative usage. Missing work is not an excuse to reset those
  records. An unfinished chapter's digest chunks are not individually checkpointed.

With book understanding enabled, every nonempty chapter needs a complete digest
before body work. A failed chunk or merge cannot publish a partial chapter digest.
A failed synopsis group invalidates the whole synopsis; stale retained analysis must
not be injected as a current synopsis. Preparation never extracts or seeds glossary
entries.

<a id="translation-checkpoints"></a>

## Stable state and translation checkpoints

`target is None` means pending. A saved empty string can be an intentional completed
MinerU translation; truthiness must not be used to decide whether to translate again.
Export fallback to source text is a separate decision and must not rewrite targets.

Preserve chapter indices, segment indices, continuation boundaries, EPUB/HTML
anchors and annotation IDs, DOCX style metadata, and `babeldoc_id`. Identity survives
batch splitting, resume, Review and export. Join long-paragraph continuations only
for operations that need the complete logical paragraph, then map results back to
the original stable segments.

For each body batch:

1. Freeze the source, full ordered glossary and consumed context.
2. Translate and optionally polish; validate shape before publication.
3. Save formal targets, then align annotations/styles.
4. Extract terms from the completed pairs and checkpoint that extraction.
5. Refresh the glossary snapshot before the next pending batch if it changed.

A completed target batch with no glossary checkpoint requires extraction, not
retranslation. A fully checkpointed batch skips both. Chapter completion follows
its required batch and chapter-level work. Keep interruption tests at the boundaries
between target persistence, alignment and glossary checkpointing.

Recent context is derived from formal targets in book order, not an independent
persisted rolling-context string. Stop at pending text; skip intentionally blank
completed targets. Lookahead is a read-only source neighbor, excluded from numbered
inputs and output count. BabelDOC role/section boundaries further restrict prose
context without deleting non-prose translation units.

The full glossary owns name mappings. It retains stable insertion order, and fresh
Reviewer requests share one complete final snapshot. Selective evidence and Fixer
requests may use relevant subsets. Japanese readings are source metadata supported
by explicit source evidence, not target-language transliterations; non-Japanese
sources do not inject them through structured glossary rendering.

Experimental terminology evidence requires both configuration and operation-scoped
permission. Only authorized CLI preparation/translation entry points grant production
access; preparation itself still does not extract terms. Ordinary Runtime/API callers
cannot gain permission by setting YAML. Person evidence publication is all-or-nothing
across required groups, bound to current entry identity, and must preserve protected
manual fields and authoritative mappings.

<a id="storage-and-artifacts"></a>

## Storage, artifacts and locks

Workflow services persist mutable state through `Storage` and `ArtifactStorage`.
They must not infer filesystem semantics from an artifact key or open a local
database to bypass the injected adapter.

| Adapter | State ownership |
| --- | --- |
| CLI file storage | Target-scoped chapter/analysis/ledger files and the local glossary |
| Desktop SQLite | Independent project database and workspace; no CLI/Web state migration |
| Web PostgreSQL | Project-scoped mutable state; upload/cache/export directories are not JSON or SQLite state mirrors |

Artifact keys are run-relative, `/`-separated names on every backend. Limit prefix
queries to the relevant subtree rather than scanning unrelated source caches.
Precision archives, policy plans and Review traces are artifacts, not portable
absolute filesystem paths. `Storage` path-shaped identity properties do not grant
domain services permission to write around the port.

Preserve the lock roles:

- The book run lock serializes long modifying workflows and recovery.
- Short state locks/transactions protect coherent reads, writes and journal preparation.
- Event appends and artifact assembly use their dedicated synchronization.
- Standalone export captures a coherent manifest/chapter snapshot under short locks,
  then renders without holding the long translation lock.

File JSON publication uses a same-directory temporary file and atomic replacement.
SQL adapters implement the equivalent transaction/locking contract; a successful
in-memory update is not a durable commit. Storage owners close resources on success
and failure. Standalone local Review reads a glossary snapshot that includes committed
WAL data without modifying the formal glossary database.

<a id="cache-identity"></a>

## Cache identity and invalidation

A cache hit means the exact consumed inputs and semantic contract remain compatible,
not merely that an artifact exists. Preserve successful records when another stage
fails; never reinterpret a diagnostic as successful work.

| Cached work | Inputs that matter |
| --- | --- |
| Parsed source | Source content and parsing configuration |
| Chapter digest/style/synopsis | Consumed source or summaries, task policy, and model configuration; synopsis also binds its actual guidance inputs |
| Translation precision | Source and stable segment plan, context, glossary, translation policy and effective inference identity |
| Annotation/style placement | Complete logical target digest and source item identities |
| Review | Reviewed content, ordered full glossary including all metadata, guidance, policy, Review options and reachable inference identities |
| Terminology requests | Entry/source identity, exact request inputs, policy and routing; publication rechecks current entry identity |

Bind only applicable inference inputs where the workflow provides an effective
operation snapshot. For example, Review should not be invalidated by an unrelated
route or concurrency-only change. A model, endpoint, consumed option or semantic
prompt change may legitimately require new requests. Completed formal translation
is not automatically discarded when a derived cache becomes incompatible.

Invalid provider/JSON/semantic responses are failures, not negative evidence caches.
A successfully validated response that supports no mapping is different: historical
term alignment can cache that unresolved decision for the exact request without
repeating calls or diagnostics. Never use cached uncertainty to overwrite a manual
glossary decision.

<a id="precision-checkpoints"></a>

## Precision checkpoints and receipts

The precision executor owns three independent concurrent drafts and one synthesis;
the chapter service alone publishes the synthesized targets. Freeze the shared
source/context prefix before starting siblings. Merge results as T1/T2/T3, not by
completion time. `target_before_polish` is T1; `target` is the synthesis.

The batch fingerprint binds source hash, stable segment plan, context, glossary,
language policy and the effective `translation.body`/`polish.body` inference
identities. Per-stage checkpoints and completed call receipts allow reuse even if
interruption occurs after a successful call but before its higher-level checkpoint.
Keep successful siblings when another draft fails.

Validate the exact output count, string types and permitted blank-target policy.
No-translation inputs retain their source text. Draft/synthesis output recovery gets
one initial attempt plus `align_retry_limit` retries for the failing stage:
structural failures add the rejected reply and corrective feedback; truncation
keeps the request unchanged. Do not silently split, zip away missing segments,
substitute a sibling, or add an accuracy judge. Provider and storage failures are
not new precision-specific transport retry loops.

Prepare a recoverable publication record before writing formal targets. Verify
source hashes, segment identities and expected targets before commit; reconcile an
interrupted ready→published transition against saved text. A ready result means
structurally valid synthesis, not semantic certification.

The `precision/` archive separates immutable shared objects and glossary versions
from batch progress. Retain exact historical request recipes, raw responses before
validation, validated outputs, available model metadata and per-call usage/events.
Missing data stays unknown, not zero. Failed calls remain failures; cache reuse is
not a new model call. Compatible historical paid outputs stay reusable even when
their replay metadata is incomplete.

Archives contain private source and translations, not credentials. They support
inspection and input reconstruction, not a deterministic model benchmark: sampling
and server-side model changes can alter a repeated response.

<a id="review-and-publication"></a>

## Review shadow state and publication

The Review engine may mutate only its run-local shadow translation and artifacts.
Every Fixer in a round reads the same immutable snapshot; patches are applied
together afterward. Blind re-review reads the new shadow without earlier issue
explanations. Evidence and arbitration recommendations do not modify the glossary.

Reviewer output requires a completion receipt matching the exact reviewed-segment
count. Do not accept truncated output by inventing missing JSON content. Invalid
receipts split only the affected chunk; single-segment output retries are bounded.
Unverified candidates or unresolved arbitration cannot bypass publication checks.
`not_rereported` records a later observation, not proof of correctness.

Reuse completed Review only when all session identities match. Resume matching
unfinished rounds, chunks, evidence turns and finished Fixer traces, including a
trace saved before its round commit. Recoverable provider/cancellation stops remain
interrupted; local/protocol failures remain diagnosable as failed. Both can resume
when their identities still match.

Autofix is a **separate, explicitly gated publication service**. The shipped
`review_autofix` setting is `true`; disabling it prevents new publication but does
not change the Review engine's shadow-only contract. The publisher:

1. Overlays folded Review changes in working text.
2. Reuses the bounded evidence loop and Fixer for remaining confirmed issues.
3. Persists the before/after chain, decisions, target hashes and write status in
   `autofix/index.json` **before** formal target writes.
4. Applies complete segment replacements only when the expected target still matches,
   recognizes already-applied replacements, and refreshes affected alignment metadata.

It adds no Review-history fields to chapters and changes neither manifest nor
glossary. An unrelated manual target edit must become a conflict, not be overwritten.
A failed additional issue fix does not undo an already valid direct change.
An interrupted committed publication index takes priority over a new Review or
revision plan, even if the caller now disables Autofix; that option is not rollback.
Recovery can still finish alignment work and incur its model usage.

Results, rounds, diagnostics and publication history remain under the Review run's
artifact prefix. Raw traces can contain private book text and model replies.

<a id="usage-ledgers"></a>

## Usage and event ledgers

Events append; usage accumulates. A Review run stores its own delta, and each
increment is merged into the book ledger **once**. Retry or resume must not add a
previously merged delta again.

The usage coordinator serializes recovery, baseline reads and journal preparation
under the state lock. It durably prepares the absolute ledger snapshots in
`usage-pending.json` before advancing its in-memory checkpoint. SQL journal
preparation commits before that advance; publication uses a separate short
transaction. A publication or event failure leaves a recoverable journal. A later
flush recovers earlier work before preparing its own increment.

Track actual available usage for successful and failed paid attempts. Do not infer
usage from batch counts, repeat a cache hit as a call, or report unavailable provider
counters as zero. Cache reads, cache writes, ordinary input and unclassified input
partition total prompt tokens; reported cache-hit rate uses reads divided by all
input, and is unknown without an available read counter. These counters are not a
price quote or proof of provider cache storage.

Timing is separate from tokens. Invocation durations accumulate once, include nested
stages, and exclude downtime between resumes. A forced kill can leave an unrecorded
duration; logs cannot reconstruct events that were never saved.

<a id="language-policies"></a>

## Language-policy lifecycle

`i18n/policy/` is the pure typed registry/resolver. Source and target bindings are
separate; language profiles inherit root-to-leaf, with exact registered pair rules.
Agents consume frozen prompt plans, domain adapters execute selected text/writer
capabilities, and pipeline services persist phase identities through Storage.

Plans freeze only consumed prompt resources and capabilities. Persist immutable
`language-policies/<fingerprint>.json` artifacts and phase checkpoints.
A semantic policy change, or missing saved identity, rebuilds affected derived
analysis before pending translation. Task-specific fingerprints preserve unchanged
digest/style caches; an interrupted rebuild must not prematurely commit the new
checkpoint. Completed targets and glossary notes remain intact.

Export identity is separate and includes actual format/backend and the consistent
source/target snapshot. Punctuation, font and ruby changes require re-export rather
than retranslation. Normalize complete logical paragraphs on an export-only copy,
remap annotation/style offsets once, and validate the complete view before opening
the output. Preserve pending, intentionally empty, and source-fallback identities.

Task instructions use English. Generated prose and descriptive metadata follow the
target language; original source names and aliases remain matching identities.
Summaries retain source identities rather than competing with the live glossary's
name mappings. JSON keys and type/gender identifiers stay stable.

Policy bindings are implementation resources, not user-editable YAML operation
overrides. A read-only diagnostic uses the same resolver without model calls:

```bash
uv run wenyi language-policy --source ja --format docx --backend native
```

Unknown built-in operations, options or handlers must fail before consuming work.

<a id="prompt-genres"></a>

## Prompt genre composition

Task, shared and language resources use trusted `${genre_*}` slots.
`i18n/genres.py` composes `genres/literary/fragments.json` defaults with the selected
genre's overrides **before** language substitution and policy freezing.
Academic fragments replace or clear literary-specific instructions; common grammar,
output protocols and source-evidence safeguards remain shared. Do not perform this
substitution on untrusted book text or add a process-global genre selector.

The two entry-point contracts differ:

- CLI's supported `--prompt-genre` option uses an invocation-local, non-serialized
  `Config.set_prompt_genre()` override carried through `PolicyContext`. The same
  choice must be supplied on resumes; Core YAML has no `pipeline.prompt_genre`.
- Web/Desktop freeze a creation-time choice in project documents and complete job
  snapshots as `pipeline.prompt_genre`. Execution restores the runtime override.
  Project edits, default restoration and resumes preserve it; missing historical
  values mean literary. It is not a global setting.

New genres register a name and trusted fragments. Omitted slots inherit literary
defaults, so explicitly clear inapplicable rules. Test composed resources across
language directions, cache fingerprints and project/job round-trips; use separate
state for quality comparisons instead of mixing completed translations.

<a id="diagnostics"></a>

## Diagnostics and verification

Distinguish provider/account failures, output-contract failures and persistence
failures. A transport or quota stop is not evidence that a candidate is wrong.
Expected user errors should be actionable without a traceback.

For terminology validation, persist operation identifiers, fixed reason codes,
counts, local indices and request-cache keys rather than rejected names, model prose,
untrusted references or raw response bodies. Do not relabel a failed request as an
empty successful result. Cache hits should not emit duplicate rejection audits.
This safe diagnostic policy does not make full event streams, Review traces or
precision archives safe to share: those may contain source and translated text.

Validate changes at the boundary they affect:

- Interrupt preparation before manifest commit and resume from successful paid work.
- Interrupt after target save, glossary extraction, precision receipt, Review round,
  Autofix index and usage-journal commits; verify idempotent recovery.
- Check content, glossary, policy and model changes invalidate only compatible scopes.
- Exercise File, SQLite and PostgreSQL adapters for storage changes, including locks
  and concurrent export snapshots.
- Use fake model/parser services and temporary data. Follow the repository's required
  architecture, orchestration, storage and affected-domain tests.

Offline checks establish contracts, not model quality. Prompt/terminology/context/
Review semantic changes need an explicit quality tradeoff and representative
public-domain comparisons before claiming an improvement.
