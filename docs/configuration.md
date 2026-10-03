# Configuration

[简体中文](zh/configuration.md)

The Wenyi CLI reads `config.yaml` from the current working directory. If the file is missing, running the CLI creates a documented default configuration.

Top-level sections are `language`, `llm`, `segment`, `pipeline`, `output`, `honorific`, and `paths`. Unknown sections are rejected; removed settings are not translated to a newer schema.

## Web/Desktop settings and model registration

The CLI continues to read `config.yaml`. Web **Settings** manages a shared registry
of provider connections and model profiles, default tiers and operation routes, and
new-project workflow defaults. The Web server reads its initial defaults from
`WENYI_CONFIG` (default `config.yaml`); after the first save, Web settings are stored in
PostgreSQL and survive restarts. Saving Web settings does not rewrite the CLI file.
API keys remain server environment variables; enter only their variable names.

Desktop uses the same settings workflow in an independent SQLite workspace, initialized
from built-in defaults rather than the CLI's current-directory file. It does not import
Web settings or projects. Desktop also accepts manually entered API keys: it automatically
uses the OS credential store when available, otherwise keeps them only for the current
session and clearly asks for re-entry after restart. Environment variables remain supported.
See [Desktop credentials](desktop.md#api-keys); keys never belong in configuration YAML.

Standard or precision translation is selected only when creating a Web/Desktop book project.
Global Settings has no translation-mode or workflow-template selector; Quick draft is
retired for new requests. Projects copy shared defaults at creation, so later default
changes do not reset an existing project's workflow or model selections. The saved
mode cannot be changed in project settings or YAML; create another project to change it.
Existing historical projects and frozen jobs retain their saved workflow.

Project configuration accepts registered model IDs through `llm.tiers`, `llm.routes`
and route fallbacks, plus `llm.budget`. Provider connections, model names/options,
presets and provider quotas belong only in global Settings. Both the project form and
its advanced YAML enforce this boundary. Old project-local registry definitions are
not used: register any project-specific profile IDs in global Settings before starting
a new task with those selections.

Registry edits apply to newly started or resumed tasks. Queued and running jobs keep
their full configuration snapshots, including provider/model parameters. Connection and
model IDs can be renamed in Settings; saving also updates model references in project
tiers, operation overrides and fallbacks in the same transaction. Historical usage and
queued job snapshots keep their original IDs. IDs start with a letter and contain only
letters, digits, underscores or hyphens. A referenced connection or model cannot be
deleted until its selections are changed. Unused registrations can be deleted.

**Restore defaults** first loads a draft, and **Save configuration** applies it. Global
Settings reloads the server configuration file (built-in defaults in Desktop).
Project settings use current global defaults while preserving the project's saved
translation mode and languages. Restoring defaults cannot
remove models still selected by other projects; change those selections first.
Operation selectors show the effective tier directly, without a “Follow default tier”
prefix. Selecting the operation's default tier clears its model override and preserves
any configured fallbacks. Concurrent global saves use a revision check;
a stale editor must reload before saving again.

## Languages

```yaml
language:
  source: auto
  target: zh
```

`source: auto` asks the model to identify the source language; alternatively, select a language below. Translation runs directly between source and target without pivoting through Chinese. Multilingual quality is experimental. The default CLI, configuration comments, and prompt instructions use English independently of the translation target. The generated configuration still defaults to `target: zh`; choose `en` for English translations.

Detection request failures are distinct from unsupported language results. HTTP 402 reports
insufficient provider balance and asks you to recharge or change provider; authentication,
rate-limit and connection failures retain their own diagnostics. `llm_request_failed` and
`language_detection_failed` events include `status_code` when available, `error_category` and
a safe `error_message`, without raw provider bodies or credentials. Setting an explicit
source skips detection but does not fix a provider/account problem for later model calls.

All generated descriptive metadata, including glossary `note`, style guidance, character descriptions, and references to characters in prose, is requested in the target language. Character `target` values contain translated or transliterated names; `source` and `aliases` preserve the original spelling for matching. Original-language quotations may appear as evidence. Type and gender values use English identifiers; older Chinese enum values are no longer converted. Matching policies reuse saved analysis and notes. A changed built-in semantic policy automatically rebuilds affected analysis; existing glossary notes are retained. Use a separate `paths.state_dir` for a complete new translation and quality comparison.

| Codes | Languages |
|---|---|
| `zh`, `zh-Hant` | Simplified and Traditional Chinese |
| `en`, `en-US`, `en-GB` | English, American English, British English |
| `ja`, `ko` | Japanese, Korean |
| `fr`, `de`, `es`, `it` | French, German, Spanish, Italian |
| `pt`, `pt-BR`, `pt-PT`, `ru` | Portuguese, Brazilian/European Portuguese, Russian |
| `vi` | Vietnamese |

Run `uv run wenyi languages` to list built-in profiles without an API key. `target` cannot be `auto`; unsupported codes fail configuration validation. Registered aliases include `zh-Hans` / `zh-CN` → `zh`, `zh-TW` → `zh-Hant`, `ja-JP` → `ja`, `ko-KR` → `ko`, and `vi-VN` → `vi`. Registered script/region variants are preserved rather than truncated to two letters.

Each invocation selects one direction. For example, `source: zh`, `target: en` translates Chinese directly into English; `source: ja`, `target: en` translates Japanese directly into English. Identical languages after detection/normalization are rejected. Changing the target creates separate state. Use the corresponding `language.target` for `prepare`, `translate`, `review`, `assemble`, `status`, `report`, and glossary commands. An explicit source conflicting with saved state is rejected on resume.

See the [pipeline guide](pipeline.md) for prompt resources and state isolation, and [Web interface languages](web-i18n.md) for display-language settings. Multilingual long-form blind evaluation, native-language review and RTL/layout certification remain future work; interface language support does not certify translation quality. The CLI and prompt instructions remain English; there are no `ui_locale` or `prompt_locale` configuration fields.

## Built-in language policies

Language policies are implementation details defined in `packages/core/wenyi_core/i18n/policy/` and `i18n/data/languages/` / `pairs/`. Developers change these resources, operation specifications and domain implementations in source, with the corresponding tests. YAML accepts only `source` and `target` under `language`; there are no operation overrides or revision-acceptance switches, and Web settings do not display policy plans.

Source markup follows the source language; target punctuation, font and metadata follow the target. Profiles inherit root-to-leaf and exact registered language-pair bindings take precedence. `zh-Hant` disables the Simplified Chinese normalizer and retains the English about-page fallback. Existing `output.punctuation_normalize`, `honorific.strategy` and workflow options remain authoritative.

Developers can inspect the same built-in resolver without credentials or model calls:

```bash
uv run wenyi language-policy --source ja --format docx --backend native
uv run wenyi language-policy --source en --subtitles --format srt
```

Diagnostics report selections, resource hashes, versions, model routes and fingerprints; they do not modify the policy. Automatic source detection remains unresolved until the source is known; use `--source` to inspect a specific direction. Unknown built-in IDs/options or unavailable handlers fail before consuming work.

On resume, changed semantic policies automatically rebuild affected chapter/style/synopsis analysis before pending work continues. Missing policy identities also require rebuilding derived analysis. Completed targets and glossary remain saved; unchanged task caches are reused, and interrupted rebuilds resume safely. Rebuilding analysis may make model calls. SRT preserves completed cues and ignores incompatible pending-window caches. Font, ruby and export punctuation changes require only a fresh export; Review uses a new policy-bound session. Export fingerprints bind the actual format/backend and consistent source/target snapshot.

## Models and operation routing

Keep the three convenient tiers, override one operation, or mix provider connections. Start with:

```yaml
llm:
  preset: deepseek
```

This preset expands to connection `default`, profiles `default_strong`, `default_cheap`, and `default_fast`, and all three tier mappings. Its product defaults are `https://api.deepseek.com`, `DEEPSEEK_API_KEY`, `deepseek-flash` for all three tiers, with thinking enabled and `reasoning_effort: high`. The model ID and reasoning defaults follow the [DeepSeek API documentation](https://api-docs.deepseek.com/api/create-chat-completion/). The tiers retain independent mappings for later overrides; presets do not query remote capabilities. `preset: gemini` and `preset: fake` are also available; fake is offline.

For independent polishing and evidence verification:

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

Replace `YOUR_EDITOR_MODEL` with a model supported by your endpoint. Other operations retain their default tier mappings; `autofix.verify` inherits the resolved `review.verify` route unless explicitly overridden.

### Configuration rules

- `providers.<id>` defines a connection: `kind`, optional `base_url`, `api_key_env`, `timeout` (seconds, default 600), `max_retries` (additional attempts, default 4), `max_concurrency` (unlimited unless set), and optional `quota_group`.
- `models.<id>` defines a request profile: `provider` connection ID, remote `model` ID, optional positive `max_output_tokens`, and adapter-specific `options`.
- `tiers` maps exactly `strong`, `cheap`, and `fast` to profiles. Without a preset, all three are required; they can select the same profile. Tier names describe preferences, not measured quality or price.
- `routes.<operation>` selects exactly one of `{model: profile}` or `{tier: strong}`. Unknown operations, fields and references fail before requests. There is no missing-tier fallback.
- Preset overrides replace whole connection/profile entries by ID. Repeat required fields when replacing an entry; model options are not merged across profiles. Tier and route mappings replace individual keys.
- An explicit `max_output_tokens` overrides static and dynamic workflow hints. Without it, synopsis and annotation hints retain their prior behavior. OpenAI-compatible thinking profiles expand hints below 4,096 to 4,096; explicit smaller caps are rejected while thinking is enabled. Actual model limits still depend on the service.
- CLI/Web API keys come only from environment variables; Desktop additionally supports its OS credential store or session-only manual input. Do not place credentials in YAML, endpoints or request overrides. Raw overrides cannot replace model identity, messages, streaming, JSON mode, credentials or output caps.

### Provider options

| Adapter kinds | Connection defaults / options | Model options |
|---|---|---|
| `deepseek` | DeepSeek endpoint; `DEEPSEEK_API_KEY` | `thinking`, `reasoning_effort`, `extra_body` |
| `openai` | OpenAI endpoint; `OPENAI_API_KEY` | `thinking`, `reasoning_effort`, `extra_body` |
| `openrouter` | OpenRouter endpoint; `OPENROUTER_API_KEY` | `thinking`, `reasoning_effort`, `extra_body` |
| `opencode-go` | OpenCode Go gateway (`https://opencode.ai/zen/go/v1`); `OPENCODE_API_KEY`. Sends `User-Agent: wenyi` and a stable per-connection `x-opencode-session`. No built-in preset — configure models explicitly | `thinking`, `reasoning_effort`, `extra_body` |
| `gemini` | Native Gemini API; `GEMINI_API_KEY`, falling back to `GOOGLE_API_KEY` when no custom variable is set | `thinking_level` or `thinking_budget`, `temperature`, `extra_body` |
| `openai-compatible` | Explicit `base_url`; optional `api_key_env`; `reasoning_style` | `thinking`, `reasoning_effort`, `json_response_fallback`, `request_overrides` |
| `orcarouter` | `https://api.orcarouter.ai/v1`; `ORCAROUTER_API_KEY`; `reasoning_style` | Same as `openai-compatible` |
| `ollama`, `vllm` | `http://localhost:11434/v1`, `http://localhost:8000/v1`; optional credentials; `reasoning_style` | Same as `openai-compatible` |
| `fake` | No network or credentials | No provider options |

Compatible endpoints accept `reasoning_style: none` (default), `deepseek`, `openai`, or `openrouter`. `json_response_fallback: reasoning_content` is an explicit option for gateways placing JSON there; the default is `none`, and non-JSON reasoning is never accepted. Gemini thinking level and thinking budget are mutually exclusive. Raw extension dictionaries are endpoint-specific; offline validation cannot prove a remote model supports them.

Provider SDK retries are disabled. Wenyi retries transient connections/timeouts, HTTP 408/409/429 and 5xx responses, and empty responses through one shared policy. Retry backoff releases the connection permit and responds to cancellation. Ordinary 4xx errors are not retried. PDF's default MinerU import uses a separate `MINERU_API_KEY`; the optional BabelDOC HTTP bridge is independent of model routing.

DeepSeek accepts `reasoning_effort: low`, `high`, or `max`; `thinking: false` explicitly disables thinking and omits the effort parameter. When neither a profile cap nor a workflow hint applies, the service supplies its default output limit: 8K without thinking, 64K with thinking, or 128K at `max` effort. Workflow hints and explicit `max_output_tokens` still follow the configuration rules above. See the [DeepSeek request parameters](https://api-docs.deepseek.com/api/create-chat-completion/).

### Registered operations

| Operation | Default tier or inheritance | Purpose |
|---|---|---|
| `language.detect` | `cheap` | Detect source language |
| `analysis.style` | `strong` | Analyze style, characters and seed glossary |
| `synopsis.chapter` | `fast` | Chapter digest; 600-token hint |
| `synopsis.book` | `fast` | Book synopsis; 1,200-token hint |
| `translation.body` | `strong` | Body translation and alignment recovery |
| `translation.title` | `strong` | Chapter and TOC titles |
| `polish.body` | `strong` | Prose polishing |
| `glossary.extract` | `fast` | Glossary extraction |
| `glossary.align_history` | `fast` | Earlier translation alignment |
| `annotation.align` | `cheap` | Annotation alignment; dynamic output hint |
| `review.scan` | `cheap` | Initial and blind review |
| `review.verify` | `strong` | Evidence verification |
| `review.arbitrate` | `strong` | Conflict arbitration |
| `review.fix` | `strong` | Shadow revision |
| `autofix.verify` | `review.verify` | Publication evidence verification |
| `autofix.fix` | `review.fix` | Publication revision |
| `srt.translate` | `strong` | Subtitle batches and single-cue recovery |

### Preview, limits and explicit failover

```bash
uv run wenyi models list
uv run wenyi models list --json
uv run wenyi models explain --operation review.verify
uv run wenyi models check --for translate
```

`list` and `explain` need no keys. `check --for prepare|translate|review|srt` validates credentials only for reachable operations, respecting the configuration's stage switches. These three commands construct no SDK clients and send no requests. Translation commands apply their CLI stage overrides before credential validation.

Optional local controls, illustrated with an offline provider:

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

Connections sharing a `quota_group` share RPM/TPM reservations within one invocation. Provider concurrency also spans every operation using that connection. These controls do not coordinate other processes or enforce an account's actual remote quota. Token controls reserve a conservative prompt-byte estimate plus an explicit output limit, then adjust it when actual usage arrives; reservations are not billed usage or a currency spending cap. Token limits require finite output limits for every reachable primary and fallback profile.

`deadline_seconds` and Ctrl+C stop queued requests and backoff cooperatively. An in-flight SDK call can finish or reach its connection timeout; completed work is retained for resume. A stopped invocation gets a new budget on restart.

For stateless requests, an explicit route may use `fallbacks: [backup_profile]`. Wenyi tries that chain only after a retryable transport failure exhausts retries. Authentication, configuration and output-schema errors do not trigger model failover. Resumable `review.verify`, `review.arbitrate`, and `autofix.verify` conversations reject failover to prevent mixed-model traces.

### Usage and resume

One ledger tracks totals with independent `by_tier`, `by_stage` (operation IDs), `by_provider`, and `by_model` views. Direct profile selections use tier `direct`. Physical identities distinguish endpoint, model and inference options even if aliases are reused; aliases and labels never determine totals. Actual response usage is retained even if parsing fails or a retry follows; responses without usage do not invent token charges.

Events record the routing plan and request operation, model, provider, profile, connection, inference fingerprint, call ID and attempt. Full-book and Review usage updates are journaled in `usage-pending.json` before publication, so an interrupted local merge can recover without counting the increment twice. A process killed after remote acceptance but before local persistence can still leave unknown remote usage.

Changing translation, analysis, synopsis or SRT models keeps completed work and uses the new route for pending calls. Review starts a new run when a reachable review model, endpoint, options or protocol changes; unrelated routes, credential rotation, alias renaming and concurrency changes do not invalidate it. Old Review caches lacking inference identity are retained but not reused. Autofix has its own fingerprint: pending indexed publication finishes from saved candidates; unfinished inference planning requires restoring its original routes before continuing.

Retired configuration and nonempty old usage ledgers require explicit conversion:

```bash
uv run wenyi models migrate-config old-config.yaml --out routed-config.yaml
uv run wenyi models migrate-usage state/BOOK/targets/zh
```

The config converter creates a separate file. The usage converter backs up each selected ledger, preserves totals and old tier/stage attribution, and assigns missing provider/model history to `unknown`. It never processes source books. Run ledger conversion while that target's workflows are stopped. Review directories are preserved. `pipeline.review_agent_tier` is replaced by the separate verification, arbitration and fix routes.

Use isolated public-domain fixtures before choosing a mixed-model setup. No new quality-ranked model preset is implied by routing support.

## Pipeline

`pipeline.translation_mode` accepts `standard` (default) or `best_of_three`.
Precision mode requires `pipeline.polish: true`; invalid combinations are rejected,
including project YAML settings. In Web, choose the translation mode
when creating a book project; new projects default to `standard`, independently of
global settings. Selecting precision enables polishing for that project. Its mode is
fixed at creation; project settings can adjust other fields but cannot disable the
polishing required by precision. Restoring defaults also preserves the saved mode.
Translation mode is not a global Web setting; global YAML rejects it. SRT does not
support precision; project YAML and source replacement enforce this boundary.

Precision always creates three drafts with built-in three-branch concurrency; it is
not a user option. Existing configurations and frozen jobs containing the retired
`pipeline.precision_concurrency` field remain readable, but the value is ignored
and omitted from new configuration documents. New Web YAML writes reject it.
One comprehensive source-aware polishing combines their useful parts and outputs
final text directly. It uses the existing `translation.body` and `polish.body`
routes, profiles and provider behavior, with no precision-specific output-token
caps or hints. A normal batch uses four calls versus two for standard translation
with polishing. The former `translation.select`, `translation.verify` and
`translation.refine` operation IDs are no longer supported: existing explicit
`llm.routes` entries for them fail validation. Remove those entries and configure
`translation.body` / `polish.body` explicitly if needed; no settings are rewritten.
See [precision workflow](pipeline.md#best-of-three-precision-translation) for
synthesis, resume, and cost semantics.

```yaml
pipeline:
  translation_mode: standard
  review: true
  polish: true
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  annotation_alignment: true
  annotation_alignment_concurrency: 4
  review_concurrency: 4
  review_output_retries: 2
  review_agent_loop: true
  review_agent_max_evidence_rounds: 2
  review_conflict_arbitration: true
  review_fix_loop: true
  review_fix_max_rounds: 2
  review_clean_confirmations: 2
  review_autofix: true
  pdf_backend: mineru
  babeldoc_bridge_url: http://127.0.0.1:8765
  babeldoc_timeout: 600
```

- `review`: enabled by default; automatically run the evidence-driven whole-book review after the complete book has been translated. Pass `--no-review` or set this to `false` to skip it in the one-command workflow. The explicit `wenyi review` command remains available.
- `polish`: run the strong model over translated batches again for style. This may improve quality but significantly increases runtime and cost.
- `rolling_context_segments`: number of recent translated segments included with each translation batch. Translation and polishing also receive one following source segment from the same chapter as a read-only reference, including when this setting is zero. This built-in lookahead does not change output counts or saved translation context; see [whole-book context](pipeline.md#whole-book-understanding-and-context).
- `book_understanding`: prescan the book to create chapter digests and a whole-book synopsis. Chapters with source text require a usable digest before body translation; synopsis synthesis failures allow translation to continue. Failed digests are retried on the next prepare/translate run. See [Pipeline](pipeline.md) for retry and cache behavior.
- `prescan_concurrency`: number of chapter-digest requests that may run concurrently.
- `annotation_alignment`: enabled by default. After each annotated logical paragraph has been fully translated and polished, immediately locate EPUB footnote/endnote links with one sequential model call against the formal target. If export punctuation normalization is enabled, the export layer remaps the persisted offsets together with the normalized in-memory copy. Split continuations are rejoined first, and segments without internal links do not call the model. When disabled, translated links remain clickable but fall back to end-of-paragraph markers; untranslated text and the source side of bilingual output retain the original link positions. This option controls link placement only; resolved source-language note content is supplied to translation automatically.
- `annotation_alignment_concurrency`: when a paragraph carries more than one annotation, each annotation is aligned through its own independent, concurrently issued request instead of asking one call to place every marker at once (a single mistake used to invalidate the whole paragraph's markers, which is why heavily annotated books tended to fall back to end-of-paragraph placement far more often). This caps how many of those per-annotation requests may run at once for a single paragraph.
- `review_concurrency`: concurrency limit for contiguous review chunks and same-round Fixer calls against an immutable translation snapshot; set it to `1` for sequential work.
- `review_output_retries`: extra attempts for a single-segment review whose output still lacks a valid completion receipt after local JSON repair and larger-chunk splitting; `2` means at most three attempts including the first call.
- `review_agent_loop`: after the unchanged initial Reviewer finds candidates in a successful leaf chunk, let an Agent Loop selectively request evidence and confirm, dismiss, or refine those candidates.
- `review_agent_max_evidence_rounds`: maximum selective evidence rounds per Agent Loop; the allowed range is `0` to `2`, after which the agent must return a final decision.
- `review_conflict_arbitration`: after all chunks finish, run a recommendation-only arbiter when consistency proposals for the same term, pronoun, or fixed expression contradict one another.
- `review_fix_loop`: generate complete provisional segment replacements for confirmed issues in a run-local shadow translation, then blindly review the whole book again. Disabling it keeps the single-pass recommendation-only behavior.
- `review_fix_max_rounds`: maximum number of provisional Fix rounds, from `0` to `4`; this is not the total number of Review passes.
- `review_clean_confirmations`: consecutive issue-free whole-book Review passes required after shadow fixing, from `1` to `2`; the default is `2`.
- `review_autofix`: enabled by default. After the read-only Review engine finishes, publish its folded `changes` to a working translation, run the existing bounded Review Agent Loop once more over each remaining issue against that updated text, and pass confirmed issues to the existing Review Fixer. Pass `--no-autofix` or set this to `false` to keep Review from writing formal `target` values. The resulting complete segments replace only the formal chapter `target`; the manifest and glossary remain unchanged. Full before/after chains, issue IDs, decisions, failures, and write status are kept in the Review run's `autofix/index.json` instead of adding history fields to chapter JSON.
- `pdf_backend`: default `mineru` converts PDF via MinerU HTML. Use `babeldoc` for layout-preserving export through the external AGPL HTTP bridge. PDF state created with BabelDOC defaults to PDF output for both `translate` and `assemble`; MinerU state retains EPUB output. Explicit `--format` overrides this choice, and saved state determines the default on resume.
- `babeldoc_bridge_url`: BabelDOC bridge base URL; default `http://127.0.0.1:8765`.
- `babeldoc_timeout`: HTTP timeout in seconds for bridge extract and fillback.
- `babeldoc_pages`: optional 1-based page selection such as `"15"` or `"6-8"`; omit it to process the whole file.

The command-line flags `--polish`, `--no-polish`, `--review`, and `--no-review`
override the corresponding configuration values for a `translate` run.

Body translation and fresh Reviewer requests always receive the full glossary; there
is no scope selector. Remove `pipeline.glossary_scope` from existing YAML or project
configuration: the retired key is rejected, including `full`. See [glossary policy](pipeline.md#glossary)
for snapshot refresh, resume behavior, and the prompt-size tradeoff.

Run final review independently with `wenyi review INPUT`. Matching content,
configuration, full-glossary policy, and glossary fingerprints reuse completed results
or resume unfinished work. Otherwise, Review starts a new run. By default, Review
publishes folded changes after the shadow loop. Use `--no-autofix` to keep that
invocation read-only, or `--autofix` to force publishing when the config is off.
Autofix first applies folded Review changes, then reuses the same Agent
Loop and Fixer for final unresolved issues; there is no separate Autofix loop or
prompt. The consolidated result and internal round records are written under
`state/<book>/targets/<target-language>/reviews/review-<timestamp>/`. Review usage is stored both as the
run-local delta and in the book's cumulative usage totals.

## Output

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  bilingual_preserve_source_style: false
  about_page: true
  punctuation_normalize: true
```

- `mono`: produce a monolingual edition as `<book-name>.<target-language>.<extension>` (`.zh.epub` normally; `.zh.pdf` for BabelDOC PDF state and `.zh.docx` for DOCX input).
- `bilingual`: request a source-and-translation edition as `<book-name>.<target-language>-bi.<extension>`, using the same selected format as monolingual output.
- `bilingual_order`: `target_first` places the translation before the source; `source_first` reverses the order.
- `bilingual_preserve_source_style`: when `true`, source blocks inherit the book's normal text style instead of using the subdued gray style. This affects EPUB and HTML output only.
- `about_page`: append an “About this translation” project page to the book; set it to `false` to disable it.
- `punctuation_normalize`: normalize punctuation only on the in-memory export copy for Simplified Chinese targets. Traditional Chinese and other targets skip this deterministic conversion. Formal chapter `target` values, Review input, and resume state remain unchanged.

The former top-level `punctuation.normalize` key is not accepted; remove it and configure only `output.punctuation_normalize`.

Only the monolingual edition is enabled by default. `--bilingual` enables both editions, and configuration plus command-line switches can be combined to produce only the bilingual edition.

## Segmentation, honorifics, and paths

```yaml
segment:
  max_tokens_per_batch: 1800
  max_tokens_per_segment: 1200

honorific:
  strategy: keep_style

paths:
  state_dir: state
```

- `max_tokens_per_batch`: source-token budget for one model translation request, counted with tiktoken `cl100k_base` (a universal estimator, not the live provider tokenizer).
- `max_tokens_per_segment`: token threshold for splitting an exceptionally long source paragraph at sentence boundaries.
- `honorific.strategy`: Japanese-source honorific policy: `keep_style`, `normalize`, or `drop`.
- `state_dir`: location of book checkpoints, chapter files, the glossary database, usage data, and reports. Subtitle runs store a separate tree at `<state_dir>/srt/<slug>/targets/<target-language>/` (manifest, cues, batches, usage, events) and never create a glossary or review directory.

All book targets, including the default `zh`, use `<state_dir>/<slug>/targets/<target-language>/`; subtitles use `<state_dir>/srt/<slug>/targets/<target-language>/`. Each directory owns its translations, glossary, context, accounting, and Review. Root-level state from earlier versions is no longer discovered or migrated. Start a new translation with the current configuration; existing files remain untouched. Saved manifests must include `source_lang`, `target_lang`, and a valid `source_sha256`.
