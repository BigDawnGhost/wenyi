# Configuration reference

This page describes settings and defaults, not screen layout or implementation
protocols. For tasks, use the [CLI](../guide/cli.md),
[Desktop](../guide/desktop.md), [Web](../guide/web.md), or
[workspace](../guide/workspace.md) guide. See the
[workflow guide](../guide/pipeline.md) for quality/cost decisions and the
[format reference](formats.md) for compatibility and output restrictions.

<a id="scope"></a>

## Configuration sources and scope

The CLI reads `config.yaml` from the current working directory. Pass
`wenyi --config PATH ...` to choose another file. A missing file is created with
documented defaults, including on help/version invocations. Omitted settings use
built-in defaults. Saved settings are [backed up and rebuilt](#retired-settings)
when they contain incompatible fields; new API/YAML submissions to Web/Desktop
and direct Core configuration validation reject unknown fields.

| Section | Controls |
|---|---|
| `language` | Source and target languages. |
| `llm` | Provider connections, model profiles, tiers, routes, quotas, and budgets. |
| `segment` | Source batching and long-paragraph splitting. |
| `pipeline` | Preparation, translation, polishing, Review, and PDF backend. |
| `output` | Editions, bilingual styling, about page, and punctuation. |
| `paths` | CLI state location. |

**Platform boundaries**

- Web initializes global defaults from `WENYI_CONFIG` (default `config.yaml`);
  saved global settings live in PostgreSQL and do not rewrite the CLI file.
- Desktop initializes settings from built-in defaults and saves them in the
  selected [data root's catalog](../guide/desktop.md#data-locations-and-backups).
  It does not read the CLI's current-directory config or import Web projects.
- Web/Desktop global settings own connections, model definitions, presets, and
  provider quotas. Project configuration selects registered model IDs through
  `llm.tiers`, `llm.routes` and fallbacks, and may set `llm.budget`; it cannot
  define a private registry. Register custom IDs globally before using them.
- Projects copy defaults at creation. Later global-default changes do not reset
  existing project choices. Registry edits affect newly started/resumed tasks;
  queued/running tasks retain their configuration snapshots.
- Web/Desktop translation mode and prompt genre are creation-time choices.
  Valid choices remain fixed when other settings or defaults change. The saved project
  field `pipeline.prompt_genre` is **not** accepted in Core/CLI YAML or global
  settings. CLI uses [`--prompt-genre literary|academic`](../guide/cli.md#prompt-genres)
  for invocation-local selection, defaulting to `literary`.
- Renaming registered IDs updates project selections, but historical usage and
  queued snapshots retain their old IDs. Referenced registrations cannot be
  deleted until those references are changed.

CLI/Web keys come only from environment variables. Desktop additionally supports
manual credentials, automatically using the OS credential store when available,
otherwise session-only memory. Never put real keys in YAML, URLs, model options,
or shared files. Credential entry, saving, and reset procedures belong in the
platform guides, especially [Desktop](../guide/desktop.md).

## Languages

| Setting | Default | Values / meaning |
|---|---|---|
| `language.source` | `auto` | Model-based detection, or an explicit supported code. |
| `language.target` | `zh` | A supported code; `auto` is not allowed. |

```yaml
language:
  source: auto
  target: zh
```

| Codes | Languages |
|---|---|
| `zh`, `zh-Hant` | Simplified and Traditional Chinese |
| `en`, `en-US`, `en-GB` | English, American English, British English |
| `ja`, `ko` | Japanese, Korean |
| `fr`, `de`, `es`, `it` | French, German, Spanish, Italian |
| `pt`, `pt-BR`, `pt-PT`, `ru` | Portuguese, Brazilian/European Portuguese, Russian |
| `vi` | Vietnamese |

`wenyi languages` lists the built-in profiles without credentials. Registered
aliases include `zh-Hans` / `zh-CN` → `zh`, `zh-TW` → `zh-Hant`,
`ja-JP` → `ja`, `ko-KR` → `ko`, and `vi-VN` → `vi`. Registered script/region
variants are preserved rather than truncated. Unsupported codes and identical
source/target languages after normalization/detection are rejected.

Translation is direct between the selected languages, with no Chinese pivot.
Generated descriptive metadata is requested in the target language; original
names and aliases remain available for matching. CLI messages and prompt
instructions remain English. There are no `ui_locale` or `prompt_locale` YAML
fields; interface language is a separate platform preference.

Changing the target selects separate state. Use the same target for preparation,
translation, Review, export, and inspection. A source language conflicting with
saved state is rejected. Setting an explicit source avoids detection, not later
provider/account errors. Multilingual quality remains experimental; RTL targets
and every format/language combination are not certified.

Built-in language policies are not configurable operation overrides. Changes may
rebuild affected analysis on resume while preserving completed translations and
glossary entries. Export-only changes need a fresh export. Developer diagnostics
and policy resources are documented in [language policies](../development/pipeline.md#language-policies).

## Models and operation routing

Connections describe where requests go; profiles describe a model request;
tiers and operation routes choose profiles. Tier names are convenient roles,
not measured quality or price rankings.

### Presets

```yaml
llm:
  preset: deepseek
```

An omitted or empty `llm` section uses the DeepSeek preset. Each preset expands to
connection `default`, profiles `default_strong`, `default_cheap`, and
`default_fast`, and all three tier mappings:

| Preset | Models in all three tiers | Model options |
|---|---|---|
| `deepseek` | `deepseek-flash` | `thinking: true`, `reasoning_effort: high` |
| `gemini` | `gemini-3.6-flash` | Thinking/temperature unset; service defaults apply. |
| `fake` | `fake` | Offline test adapter, no real translation or credentials. |

Other provider kinds have no built-in preset: configure explicit profiles.
Presets do not query the remote service for capabilities.

<a id="model-profiles"></a>

### Connections, profiles, and routes

| Setting | Default / requirement |
|---|---|
| `llm.preset` | Optional; use an available preset, or define the registry and all tiers explicitly. |
| `llm.providers.<id>.kind` | Required registered provider kind. |
| `llm.providers.<id>.base_url` | Adapter default, or explicit absolute HTTP(S) URL; no embedded credentials, query, or fragment. |
| `llm.providers.<id>.api_key_env` | Adapter default environment-variable name; stores a name, not a key. |
| `llm.providers.<id>.timeout` | `600` seconds; positive finite number. |
| `llm.providers.<id>.max_retries` | `4` additional transport attempts; integer ≥ 0. |
| `llm.providers.<id>.max_concurrency` | Unset/unlimited; positive integer if supplied. |
| `llm.providers.<id>.quota_group` | Unset; otherwise reference a configured quota group. |
| `llm.models.<id>.provider` | Required connection ID. |
| `llm.models.<id>.model` | Required remote model name. |
| `llm.models.<id>.max_output_tokens` | Unset; positive integer if supplied. |
| `llm.models.<id>.options` | `{}`; adapter-specific fields below. |
| `llm.tiers` | Exactly `strong`, `cheap`, and `fast`, each mapped to a profile. |
| `llm.routes.<operation>.model` / `.tier` | Exactly one selection on each explicit route. Omitted routes use the operation defaults. |
| `llm.routes.<operation>.fallbacks` | `[]`; ordered profile IDs, subject to failover restrictions below. |
| `llm.quotas` | `{}`; optional named quota groups. |
| `llm.budget` | No limits unless supplied. |

Provider, model, and quota IDs start with a letter and contain only letters,
digits, underscores, or hyphens. Unknown operations, fields, and references in
the routing schema fail validation. Tiers can all point to the same profile,
but none may be missing.

Preset overrides replace **whole connection/profile entries** by ID: repeat
required fields when replacing one. Model options are not merged with the old
profile. Tiers and routes are selected per key.

For example, keep the default translation route and use another model for
polishing and evidence verification:

```yaml
llm:
  preset: deepseek
  providers:
    editorial:
      kind: gemini
      api_key_env: GEMINI_API_KEY
      timeout: 120
      max_retries: 2
      max_concurrency: 2
  models:
    editor:
      provider: editorial
      model: YOUR_EDITOR_MODEL
      max_output_tokens: 8192
      options:
        thinking_level: high
  routes:
    polish.body: {model: editor}
    review.verify: {model: editor}
```

Replace the model placeholder with one supported by the endpoint.
`autofix.verify` inherits the effective `review.verify` route unless overridden.
Use `wenyi models list`, `models explain --operation ID`, and
`models check --for prepare|translate|review` for local checks.
Only `models ping` sends live probes; see the [CLI reference](../guide/cli.md#model-commands).

### Provider options

| Kind | Default endpoint | Default credential |
|---|---|---|
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `opencode-go` | `https://opencode.ai/zen/go/v1` | `OPENCODE_API_KEY` |
| `gemini` | Native API, `https://generativelanguage.googleapis.com` | `GEMINI_API_KEY`, then `GOOGLE_API_KEY` when no custom variable is set |
| `openai-compatible` | Explicit `base_url` required | Optional `api_key_env` |
| `orcarouter` | `https://api.orcarouter.ai/v1` | `ORCAROUTER_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | Optional `api_key_env` |
| `vllm` | `http://localhost:8000/v1` | Optional `api_key_env` |
| `fake` | No network | None |

| Model option | Applicable kinds | Default / constraints |
|---|---|---|
| `thinking` | DeepSeek, OpenAI, OpenRouter | `true` |
| `thinking` | OpenCode Go and compatible kinds | `false` |
| `reasoning_effort` | All preceding kinds | `high`; accepted remote values depend on the endpoint/model. |
| `extra_body` | DeepSeek, OpenAI, OpenRouter, OpenCode Go, Gemini | `{}`; endpoint-specific request extensions. |
| `thinking_level` | Gemini | Unset; mutually exclusive with `thinking_budget`. |
| `thinking_budget` | Gemini | Unset; mutually exclusive with `thinking_level`. |
| `temperature` | Gemini | Unset. |
| `json_response_fallback` | Compatible kinds | `none`, or explicit `reasoning_content` for gateways returning valid JSON there. |
| `request_overrides` | Compatible kinds | `{}`; endpoint-specific request extensions. |

“Compatible kinds” means `openai-compatible`, `orcarouter`, `ollama`, and `vllm`.
They additionally accept **connection-level**
`llm.providers.<id>.reasoning_style`: `none` (default), `deepseek`, `openai`,
or `openrouter`. `fake` accepts no model options.
OpenCode Go sends Wenyi identification/session headers automatically.

DeepSeek supports effort values `low`, `high`, and `max`; `thinking: false`
disables thinking and omits effort. When no explicit output cap is configured,
the service defaults documented by DeepSeek are 8K without thinking, 64K with
thinking, or 128K at `max` effort; consult its
[request parameters](https://api-docs.deepseek.com/api/create-chat-completion/)
for current service limits.

Workflow operations do not silently add output-token caps, including summaries
and annotation alignment. Without `max_output_tokens`, the request omits the cap
and uses the service default—not unlimited output. OpenAI-compatible transport
adapters reject an explicit cap below 4096 when thinking is enabled.
Connectivity probes have a separate small budget.

Raw extension dictionaries cannot replace model identity, messages, streaming,
JSON mode, credentials, timeouts/retries, or output caps. Offline validation
cannot prove that a remote model supports an otherwise valid option.

Compatible adapters negotiate JSON formatting during task requests when an
endpoint explicitly rejects the initial format. There is no saved JSON-mode
preference to configure, and no fallback to unconstrained text. Connectivity
tests use plain text and do not certify structured task output.

Wenyi owns transport retries; SDK retries are disabled. Connections/timeouts,
HTTP 408/409/429, 5xx responses, and empty responses are retryable; ordinary 4xx
errors are not. Retry waiting releases the connection slot and responds to
cancellation. Output-structure retries are controlled separately by the pipeline.

MinerU PDF parsing is independent of these connections and uses
`MINERU_API_KEY` in CLI/Web. Desktop checks that environment variable first,
then its manual credential; see [Desktop credentials](../guide/desktop.md).
Only an uncached MinerU conversion needs the key. Credentials do not belong in
project settings or parser caches. BabelDOC is a separate HTTP service.

### Registered operations

These are the complete current routing IDs. A route may be configured even when
its stage is disabled; reachable checks follow the selected workflow switches.

| Operation | Default tier / inheritance | Purpose |
|---|---|---|
| `language.detect` | `cheap` | Source-language detection |
| `analysis.style` | `strong` | Style and character-voice analysis |
| `synopsis.chapter` | `fast` | Chapter digest |
| `synopsis.book` | `fast` | Whole-book synopsis |
| `translation.body` | `strong` | Body translation, including precision drafts |
| `translation.title` | `strong` | Chapter and TOC titles |
| `polish.body` | `strong` | Polishing, including precision synthesis |
| `glossary.extract` | `fast` | Glossary extraction |
| `glossary.align_history` | `fast` | Earlier translation alignment |
| `terminology.discover` | `strong` | Source-grounded person discovery |
| `terminology.evidence` | `strong` | Whole-book source evidence |
| `terminology.merge` | `strong` | Evidence-note synthesis |
| `annotation.align` | `cheap` | Annotation placement |
| `review.scan` | `cheap` | Initial Review and blind rechecks |
| `review.verify` | `strong` | Evidence verification |
| `review.arbitrate` | `strong` | Contradictory recommendation arbitration |
| `review.fix` | `strong` | Run-local shadow revision |
| `autofix.verify` | Resolved `review.verify` route | Publication evidence verification |
| `autofix.fix` | Resolved `review.fix` route | Publication revision |

<a id="limits-and-failover"></a>

### Quotas, budgets, and failover

| Setting | Default | Constraint |
|---|---|---|
| `llm.quotas.<id>.requests_per_minute` | Unset | Positive integer |
| `llm.quotas.<id>.tokens_per_minute` | Unset | Positive integer |
| `llm.budget.max_requests` | Unset | Positive integer |
| `llm.budget.max_tokens` | Unset | Positive integer |
| `llm.budget.deadline_seconds` | Unset | Positive finite seconds |

```yaml
llm:
  preset: fake
  providers:
    default:
      kind: fake
      max_concurrency: 2
      quota_group: account
  models:
    bounded:
      provider: default
      model: fake
      max_output_tokens: 2048
  tiers: {strong: bounded, cheap: bounded, fast: bounded}
  quotas:
    account:
      requests_per_minute: 20
      tokens_per_minute: 60000
  budget:
    max_requests: 100
    max_tokens: 200000
    deadline_seconds: 900
```

This offline example illustrates limits, not a production translation model.
Connections sharing a `quota_group` share reservations within the invocation.
Connection concurrency spans all operations using it. Neither coordinates other
processes nor replaces the provider's account limits.

Token limits reserve a conservative prompt-byte estimate plus the configured
output cap, then adjust against returned usage. Every reachable primary and
fallback profile needs a finite output cap when token limits are enabled.
Reservations are not billed usage or a currency spending cap.
Deadline/Ctrl+C cancellation is cooperative; in-flight requests may finish or
time out. Restarting creates a new invocation budget.

For eligible stateless operations, an explicit route can include
`fallbacks: [backup_profile]`. The profiles must exist and cannot repeat the
primary or each other. The chain is used only after retryable transport failures
exhaust retries—not for authentication, configuration, or output-schema errors.
Resumable `review.verify`, `review.arbitrate`, and `autofix.verify` conversations
reject failover. Inherited routes also inherit their fallback selection.

<a id="routing-and-resume"></a>

### Changing routes and resuming

Changing translation, analysis, or synopsis models preserves completed work and
uses new routes for pending calls. Matching Review identities can reuse or resume
results; changing a relevant model, endpoint, inference option, or protocol starts
a new Review. Unrelated routes, credential rotation, alias renaming, and
concurrency changes do not by themselves invalidate it.
Unfinished Autofix inference planning requires its original routes; already
indexed publication can finish from saved candidates.

Usage remains cumulative, with independent tier, operation, provider, and model
views; direct profile selections use tier `direct`. Model/endpoint/options,
not aliases alone, distinguish inference identities. Failed parsing and retries
can still consume tokens. Missing provider counters are not invented.
Use [CLI inspection and migrations](../guide/cli.md#automation); ledger internals
are covered in [pipeline development](../development/pipeline.md).

## Pipeline

The tables below list every current Core `pipeline` setting. CLI switches override
only the values they explicitly expose; Web/Desktop project restrictions are
described under [scope](#scope).

<a id="translation"></a>

### Preparation and translation

| Setting | Default | Meaning |
|---|---|---|
| `pipeline.translation_mode` | `standard` | `standard` or `three_draft_synthesis`; the latter requires `polish: true`. |
| `pipeline.polish` | `true` | Polish translated batches; adds substantial model work. |
| `pipeline.align_retry_limit` | `2` | Additional structural-output attempts; `0` disables them. |
| `pipeline.rolling_context_tokens` | `512` | Preceding translation budget, integer ≥ 0, counted with `cl100k_base`. |
| `pipeline.book_understanding` | `true` | Generate full-source chapter digests and a book synopsis. |
| `pipeline.prescan_concurrency` | `4` | Chapter-digest concurrency; use `1` for sequential work. |
| `pipeline.terminology_context` | `false` | CLI-only experimental whole-book evidence for person entries. |
| `pipeline.annotation_alignment` | `true` | Align translated links/annotations. EPUB uses paragraph-end fallbacks; PDF omits failed items. See [PDF formats](formats.md#babeldoc). |
| `pipeline.annotation_alignment_concurrency` | `4` | Concurrent per-annotation requests within one logical paragraph. |

`three_draft_synthesis` produces **three independent drafts plus one source-aware
synthesis**, not a winner-selection contest. It uses `translation.body` and
`polish.body`; a normal batch makes four calls versus two for standard translation
with polishing. Three-draft concurrency is built in, not a user setting.
Keep the mode/configuration consistent to reuse checkpoints. Structural retry
exhaustion pauses precision work without paragraph splitting; standard translation
can fall back to individual paragraphs. Retries also consume usage.

Rolling context selects the newest contiguous translations that fit, including
newline separators; an oversized newest paragraph contributes a Unicode-safe tail.
Zero removes preceding translated context, not the built-in following-source
reference. It is reconstructed from saved translations.

`book_understanding` increases initial prescan work, especially for long chapters.
Chapters containing source text need usable digests before translation; a failed
whole-book synopsis does not block translation. Failed digests can be completed
on a later prepare/translate run. Preparation never extracts or pre-fills glossary
entries, with or without `terminology_context`.

Terminology evidence adds person discovery and source-grounded notes during
pending CLI translation work. Other term types retain ordinary extraction.
It does not create multiple senses or per-occurrence target choices.
Web/API do not produce this evidence merely because the YAML flag is set.
Review can consume existing notes without producing new evidence.

Body translation and fresh Reviewer requests always receive the full glossary;
there is no scope selector. Annotation alignment may add model requests;
placement-failure behavior depends on the [format](formats.md). Full workflow, cost, and resume
semantics belong in the [workflow guide](../guide/pipeline.md); markup/layout
limitations belong in [formats](formats.md).

<a id="review"></a>

### Review and publication

| Setting | Default | Meaning / range |
|---|---|---|
| `pipeline.review` | `true` | Run whole-book Review after complete translation. |
| `pipeline.review_concurrency` | `4` | Concurrent Review chunks and same-round Fixer calls; `1` is sequential. |
| `pipeline.review_output_retries` | `2` | Additional attempts for malformed single-segment Review output; `0`–`5`. |
| `pipeline.review_agent_loop` | `true` | Verify initial candidates with selectively requested evidence. |
| `pipeline.review_agent_max_evidence_rounds` | `2` | Evidence rounds per loop; `0`–`2`. |
| `pipeline.review_conflict_arbitration` | `true` | Arbitrate contradictory consistency recommendations after evidence review. |
| `pipeline.review_fix_loop` | `true` | Revise a run-local shadow translation and blindly review it again. |
| `pipeline.review_fix_max_rounds` | `2` | Provisional Fix rounds; `0`–`4`, not the total Review pass count. |
| `pipeline.review_clean_confirmations` | `2` | Consecutive clean whole-book confirmations; `1`–`2`. |
| `pipeline.review_autofix` | `true` | Publish Review revisions to formal chapter translations. |

**Review may change formal translations by default.** Shadow revisions and
publication are distinct: disabling `review_fix_loop` does not disable Autofix.
For reports without starting new formal writeback, set `pipeline.review_autofix: false`, or
run `wenyi review INPUT --no-autofix`. `--autofix` forces publication when YAML
has disabled it; without either flag, the command inherits YAML.
An already-started Autofix publication with a saved `applying` record is recovered
before this switch is checked, so disabling it does not cancel pending writeback.

Autofix publishes folded Review changes and can verify/fix remaining issues
against the updated text. It changes chapter translations, not the glossary.
Read the consolidated result and publication record before relying on it.
Matching content, Review configuration/model identity, guidance, and glossary
allow reuse or resume; Review does not always start a new timestamped run.

`translate --polish/--no-polish` and `--review/--no-review` override their
respective fields. `translate` has no `--no-autofix` flag: use YAML to keep its
automatic Review from starting new publication, or skip that Review and run the
standalone command with `--no-autofix`. See [Review and Autofix](../guide/pipeline.md#review-and-autofix).

<a id="pdf"></a>

### PDF backend

| Setting | Default | Meaning |
|---|---|---|
| `pipeline.pdf_backend` | `mineru` | `mineru` or `babeldoc`. |
| `pipeline.babeldoc_bridge_url` | `http://127.0.0.1:8765` | External BabelDOC HTTP bridge. |
| `pipeline.babeldoc_timeout` | `600` | Extract/fillback HTTP timeout in seconds. |
| `pipeline.babeldoc_pages` | Unset (`null`) | Optional 1-based selection, such as `"15"` or `"6-8"`; unset means all pages. |

MinerU converts PDF to HTML and requires its separate credential for uncached
imports. BabelDOC is an optional independent AGPL service for layout-preserving
PDF work. Saved parser identity determines export eligibility/defaults;
changing this setting does not convert an existing project's backend.
See [PDF requirements and limitations](formats.md#pdf), not the model-routing table,
for choosing the backend.

## Output

| Setting | Default | Meaning |
|---|---|---|
| `output.mono` | `true` | Produce a monolingual edition. |
| `output.bilingual` | `false` | Also produce a source-and-translation edition. |
| `output.bilingual_order` | `target_first` | `target_first` or `source_first`; selects the first version in paragraph-paired formats and the left-hand version in PDF spreads. |
| `output.bilingual_preserve_source_style` | `false` | Preserve recorded source styles rather than the format's default source presentation. |
| `output.about_page` | `true` | Add the project “About this translation” page to EPUB output. |
| `output.punctuation_normalize` | `true` | Normalize Simplified Chinese punctuation on the export copy only. |

`bilingual_preserve_source_style` is independent of ordering. In EPUB/HTML,
`true` inherits normal book styling instead of subdued source text. In DOCX,
`false` uses normal body styling; `true` restores recorded character styles,
including mixed spans in paragraphs/tables. It cannot recover styles never
recorded from the source.

Punctuation normalization does not modify saved chapter translations or Review
input. Traditional Chinese and other targets skip this Simplified Chinese
conversion. The EPUB about page is Chinese for Simplified Chinese targets and
otherwise currently uses English.

CLI `--mono/--no-mono` and `--bilingual/--no-bilingual` override these edition
defaults for `translate` and `assemble`. With default YAML, `--bilingual` produces
both editions; `--no-mono --bilingual` requests only the bilingual edition.
Format selection and output paths are command options, not `output.format` or
`output.path` settings. See [formats](formats.md) for naming and availability.

## Segmentation

| Setting | Default | Meaning / range |
|---|---|---|
| `segment.max_tokens_per_batch` | `1800` | Source-token budget per translation batch; integer ≥ 1. |
| `segment.max_tokens_per_segment` | `1200` | Threshold for splitting long source paragraphs; integer ≥ 0, with `0` disabling splitting. |

Both use tiktoken `cl100k_base`, a common estimator rather than the live
provider's tokenizer. They are source-packing budgets, not output-token caps.

## Honorifics

Honorific handling follows the source/target language policy automatically; it
is not a separate configuration choice. Language-pair rules take precedence
over the shared guidance, preserving relationships and tone using natural target
language conventions. For example, Japanese-to-Chinese guidance covers forms
such as `先輩 → 前辈`. There is no `honorific` configuration section.

<a id="paths"></a>

## Paths and saved state

`paths.state_dir` defaults to `state`. Relative paths are interpreted from the
CLI working directory. Targets use
`<state_dir>/<slug>/targets/<target-language>/`, each with its own translations,
glossary, usage, timing, and Review data.

Keep the same directory, source content, and target configuration when resuming.
For isolated comparisons or a fresh translation, choose a new directory rather
than overwriting existing work. Historical state at the book root is not
automatically discovered or migrated. Saved state needs valid source/target
languages and a source-content hash. See [CLI resume](../guide/cli.md#resume).

<a id="retired-settings"></a>

## Retired settings and migration

When the CLI loads a saved YAML file, or Web/Desktop starts with saved global
and project settings, incompatible configuration is rebuilt from the current
schema:

1. Preserve the original settings before replacing anything.
2. Keep valid fields and values. Discard removed fields, invalid values, and
   dependent selections that can no longer be validated. Missing fields use
   current defaults; project model selections are checked against the global registry.
3. Do not translate obsolete names into aliases. For example, `best_of_three`
   is discarded and the mode defaults to `standard`, **not**
   `three_draft_synthesis`. Select the new mode explicitly when creating a project
   or configuring a CLI run.

CLI backups are exact copies beside the selected file, named
`<filename>.backup-<sha256>.yaml`; comments remain in the backup, while rebuilt
YAML contains supported settings only. Web backups live in PostgreSQL and
Desktop backups in the selected data root's catalog, alongside the settings
they protect. Web also backs up incompatible bootstrap YAML in PostgreSQL
without rewriting the mounted file. Unchanged settings are not rewritten, and
repeated startup does not duplicate backups.

If a backup or replacement cannot be saved, the original settings stay intact
and loading/startup fails. Malformed YAML must be corrected rather than guessed.
Reconstruction does not change source books, chapter translations, usage, or
frozen job snapshots. A queued job with an incompatible old snapshot must be
started again under the rebuilt settings, not silently run with a different mode.
New Web/Desktop setting submissions and direct Core validation remain strict.

Use the backup to recover valid custom choices or run an explicit converter
where one exists:

| Retired setting / behavior | Action |
|---|---|
| Old single-provider `llm.provider` / connection fields and inline tier definitions | Run `models migrate-config SOURCE --out NEW_FILE`; inspect the separate result. |
| `pipeline.review_agent_tier` | Use `review.verify`, `review.arbitrate`, and `review.fix` routes; the legacy config converter maps this field. |
| `pipeline.rolling_context_segments` | Replace with `rolling_context_tokens`; there is no automatic segment-to-token conversion. |
| `pipeline.glossary_scope` | Remove it, including `full`; full-glossary behavior is fixed. |
| Top-level `punctuation.normalize` | Replace with `output.punctuation_normalize`. |
| `pipeline.translation_mode: best_of_three` | Removed without an alias; the current three-draft mode is `three_draft_synthesis`. |
| `pipeline.precision_concurrency` | Discarded; initial-draft concurrency is built in. |
| `honorific.strategy` | Discarded with the `honorific` section; handling follows the language pair. |
| `translation.select`, `translation.verify`, `translation.refine` route IDs | Remove them; precision uses `translation.body` and `polish.body`. |
| Web/Desktop Quick draft workflow | Not available for new requests; historical projects/jobs retain their saved workflow. |

The converter handles the retired model schema, not every row above, and never
rewrites source books. Nonempty pre-routing usage ledgers require
`models migrate-usage RUN_DIR`; it backs up the selected CLI ledgers, retains
totals, and marks unknown provider/model history as `unknown`.
Schema-2 cache accounting is read compatibly and saved on the next normal write;
it does not reconstruct missing historic cache classifications.
See [migration commands and precautions](../guide/cli.md#migrations).
