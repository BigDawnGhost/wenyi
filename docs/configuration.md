# Configuration

[简体中文](zh/configuration.md)

Wenyi reads `config.yaml` from the current working directory. If the file is missing, running the program creates a documented default configuration.

Top-level sections are `language`, `llm`, `segment`, `pipeline`, `output`, `honorific`, and `paths`. Unknown sections are rejected; removed settings are not translated to a newer schema.

## Languages

```yaml
language:
  source: auto
  target: zh
```

`source: auto` asks the model to identify the source language; alternatively, select a language below. Translation runs directly between source and target without pivoting through Chinese. Multilingual quality is experimental. The default CLI, configuration comments, and prompt instructions use English independently of the translation target. The generated configuration still defaults to `target: zh`; choose `en` for English translations.

All generated descriptive metadata, including glossary `note`, style guidance, character descriptions, and references to characters in prose, is requested in the target language. Character `target` values contain translated or transliterated names; `source` and `aliases` preserve the original spelling for matching. Original-language quotations may appear as evidence. Type and gender values use English identifiers; older Chinese enum values are no longer converted. Resuming an existing project retains its analysis and notes, so changing prompts does not automatically translate old metadata. Use a separate `paths.state_dir` for a fresh analysis and whole-book comparison.

| Codes | Languages |
|---|---|
| `zh`, `zh-Hant` | Simplified and Traditional Chinese |
| `en`, `en-US`, `en-GB` | English, American English, British English |
| `ja`, `ko` | Japanese, Korean |
| `fr`, `de`, `es`, `it` | French, German, Spanish, Italian |
| `pt`, `pt-BR`, `pt-PT`, `ru` | Portuguese, Brazilian/European Portuguese, Russian |

Run `uv run trans-novel languages` to list built-in profiles without an API key. `target` cannot be `auto`; unsupported codes fail configuration validation. Registered aliases include `zh-Hans` / `zh-CN` → `zh`, `zh-TW` → `zh-Hant`, `ja-JP` → `ja`, and `ko-KR` → `ko`. Registered script/region variants are preserved rather than truncated to two letters.

Each invocation selects one direction. For example, `source: zh`, `target: en` translates Chinese directly into English; `source: ja`, `target: en` translates Japanese directly into English. Identical languages after detection/normalization are rejected. Changing the target creates separate state. Use the corresponding `language.target` for `prepare`, `translate`, `review`, `assemble`, `status`, `report`, and glossary commands. An explicit source conflicting with saved state is rejected on resume.

See [P10 internationalization implementation and follow-up design](project-review/2026-09-05/p10-multilingual-internationalization.md) for resource layout, state layout, and validation limits.

## Model provider

```yaml
llm:
  provider: deepseek
```

Selecting `deepseek` is enough for the built-in defaults:

- Base URL: `https://api.deepseek.com`
- API key environment variable: `DEEPSEEK_API_KEY`
- Strong tier: `deepseek-v4-pro`
- Cheap and fast tiers: `deepseek-v4-flash`

API keys are always read from environment variables so they are not accidentally committed with the configuration. Use `provider: fake` for offline tests that must not make network requests.

The first PDF import with the default MinerU backend also reads `MINERU_API_KEY` to call the MinerU conversion service. This key is independent of the LLM provider and is not written to `config.yaml`. The optional BabelDOC backend does not use this key.

Add the advanced fields only when you need a proxy, custom environment variable, timeout, retry policy, or model override:

```yaml
llm:
  provider: deepseek
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: deepseek-v4-pro
      options:
        reasoning_effort: high
        thinking: true
    cheap:
      model: deepseek-v4-flash
      options:
        reasoning_effort: high
        thinking: true
    fast:
      model: deepseek-v4-flash
      options:
        thinking: true
```

`max_retries` is the number of additional attempts managed by Wenyi itself. Provider SDK retries are disabled to prevent nested requests. Wenyi retries transient transport failures, HTTP 408/409/429 and 5xx responses, plus empty model responses; each wait is recorded in the book's `events.jsonl`.

Configured tiers override the corresponding provider defaults; omitted tiers continue to use their defaults. When a requested tier is unavailable, Wenyi follows the fallback chain `fast -> cheap -> strong`.

The selected provider owns and validates the contents of `options`. In the example above, `thinking` and `reasoning_effort` are DeepSeek-specific and do not belong to the common LLM interface.

### OpenAI and OpenRouter

OpenAI and OpenRouter have dedicated providers that select their own default Base URL, API key environment variable, request fields, and reasoning format. Their model tiers must be configured explicitly:

```yaml
llm:
  provider: openrouter
  tiers:
    strong:
      model: anthropic/claude-opus-4.6
      options:
        thinking: true
        reasoning_effort: high
    cheap:
      model: openai/gpt-5-mini
      options:
        thinking: true
        reasoning_effort: medium
    fast:
      model: google/gemini-3-flash
      options:
        thinking: false
```

The OpenAI provider reads `OPENAI_API_KEY`; OpenRouter reads `OPENROUTER_API_KEY`. Both providers allow `base_url` and `api_key_env` to override their defaults.

### OrcaRouter

OrcaRouter exposes an OpenAI-compatible endpoint. The built-in `orcarouter`
provider uses `https://api.orcarouter.ai/v1` and reads `ORCAROUTER_API_KEY` by
default. [Create an OrcaRouter API key](https://api.orcarouter.ai/ref/ref_262c8b8e6a274286a90a),
then configure the model IDs available to your account:

```bash
export ORCAROUTER_API_KEY=sk-orca-...
```

```yaml
llm:
  provider: orcarouter
  tiers:
    strong:
      model: your-model-id
    cheap:
      model: your-cheap-model-id
    fast:
      model: your-fast-model-id
```

Model tiers must be configured explicitly. OrcaRouter uses the generic
OpenAI-compatible options described below, including `reasoning_style` and
per-tier `request_overrides`. You may override `base_url` or `api_key_env` when
needed.

### Google Gemini

Google Gemini is supported natively through the official `google-genai` SDK using `provider: gemini` (or `provider: google`). It reads `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) from environment variables:

```yaml
llm:
  provider: gemini
  api_key_env: GEMINI_API_KEY
  tiers:
    strong:
      model: gemini-3.6-flash
    cheap:
      model: gemini-3.6-flash
    fast:
      model: gemini-3.6-flash
```

Gemini options also support `thinking_level` (e.g. `low`, `high`) or `thinking_budget` (in tokens) for Gemini reasoning models.

### Other OpenAI-compatible endpoints

Use `openai-compatible` for any endpoint implementing OpenAI Chat Completions:

```yaml
llm:
  provider: openai-compatible
  base_url: https://api.example.com/v1
  api_key_env: EXAMPLE_API_KEY
  # deepseek | openai | openrouter | none
  reasoning_style: deepseek
  tiers:
    strong:
      model: provider-model-name
      options:
        thinking: true
        reasoning_effort: high
        request_overrides:
          thinking:
            budget: 8192
```

`reasoning_style` converts the common `thinking` and `reasoning_effort` options into the request dialect accepted by the endpoint:

- `deepseek`: `thinking.type` plus `reasoning_effort`
- `openai`: `reasoning_effort`, with `none` sent when reasoning is disabled
- `openrouter`: `reasoning.effort`, with `reasoning.enabled: false` sent when disabled
- `none`: no conversion, for endpoints that rely on model defaults or custom request fields

By default Wenyi trusts only the standard `content` response field and retries an empty response. Set `json_response_fallback: reasoning_content` on each applicable tier only for endpoints known to place the final JSON answer in `reasoning_content`; Wenyi then accepts that field only when it contains one complete JSON value.

```yaml
llm:
  provider: openai-compatible
  tiers:
    strong:
      model: provider-model-name
      options:
        json_response_fallback: reasoning_content
```

`request_overrides` is an escape hatch for provider-specific fields that Wenyi does not know about. Its contents are merged recursively into the raw top-level request body after the selected reasoning dialect is generated. For example, an endpoint using `enable_thinking: true` can be configured as follows:

```yaml
llm:
  provider: openai-compatible
  base_url: https://api.example.com/v1
  reasoning_style: none
  tiers:
    strong:
      model: provider-model-name
      options:
        thinking: true
        request_overrides:
          enable_thinking: true
```

Choose a reasoning dialect according to the endpoint protocol, not the underlying model name. A relay serving a DeepSeek model should still use `reasoning_style: openai` when that relay expects OpenAI reasoning fields.

Local Ollama and vLLM endpoints are available through the `ollama` and `vllm` providers. Their default addresses are `http://localhost:11434/v1` and `http://localhost:8000/v1`, and neither requires an API key by default. Both require explicit model tiers. Ollama's OpenAI-compatible endpoint may use `reasoning_style: openai`; vLLM reasoning support depends on the model template and server arguments. When necessary, pass `enable_thinking` through `request_overrides.chat_template_kwargs`.

## Pipeline

```yaml
pipeline:
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
  review_agent_tier: strong
  review_agent_max_evidence_rounds: 2
  review_conflict_arbitration: true
  review_fix_loop: true
  review_fix_max_rounds: 2
  review_clean_confirmations: 2
  review_autofix: true
  glossary_scope: chapter
  pdf_backend: mineru
  babeldoc_bridge_url: http://127.0.0.1:8765
  babeldoc_timeout: 600
```

- `review`: enabled by default; automatically run the evidence-driven whole-book review after the complete book has been translated. Pass `--no-review` or set this to `false` to skip it in the one-command workflow. The explicit `trans-novel review` command remains available.
- `polish`: run the strong model over translated batches again for style. This may improve quality but significantly increases runtime and cost.
- `rolling_context_segments`: number of recent translated segments included with each translation batch.
- `book_understanding`: prescan the book to create chapter digests and a whole-book synopsis.
- `prescan_concurrency`: number of chapter-digest requests that may run concurrently.
- `annotation_alignment`: enabled by default. After each annotated logical paragraph has been fully translated and polished, immediately locate EPUB footnote/endnote links with one sequential model call against the formal target. If export punctuation normalization is enabled, the export layer remaps the persisted offsets together with the normalized in-memory copy. Split continuations are rejoined first, and segments without internal links do not call the model. When disabled, translated links remain clickable but fall back to end-of-paragraph markers; untranslated text and the source side of bilingual output retain the original link positions. This option controls link placement only; resolved source-language note content is supplied to translation automatically.
- `annotation_alignment_concurrency`: when a paragraph carries more than one annotation, each annotation is aligned through its own independent, concurrently issued request instead of asking one call to place every marker at once (a single mistake used to invalidate the whole paragraph's markers, which is why heavily annotated books tended to fall back to end-of-paragraph placement far more often). This caps how many of those per-annotation requests may run at once for a single paragraph.
- `review_concurrency`: concurrency limit for contiguous review chunks and same-round Fixer calls against an immutable translation snapshot; set it to `1` for sequential work.
- `review_output_retries`: extra attempts for a single-segment review whose output still lacks a valid completion receipt after local JSON repair and larger-chunk splitting; `2` means at most three attempts including the first call.
- `review_agent_loop`: after the unchanged initial Reviewer finds candidates in a successful leaf chunk, let an Agent Loop selectively request evidence and confirm, dismiss, or refine those candidates.
- `review_agent_tier`: model tier used by the evidence loop, cross-chunk arbiter, and provisional Review Fixer. The default is `strong`.
- `review_agent_max_evidence_rounds`: maximum selective evidence rounds per Agent Loop; the allowed range is `0` to `2`, after which the agent must return a final decision.
- `review_conflict_arbitration`: after all chunks finish, run a recommendation-only arbiter when consistency proposals for the same term, pronoun, or fixed expression contradict one another.
- `review_fix_loop`: generate complete provisional segment replacements for confirmed issues in a run-local shadow translation, then blindly review the whole book again. Disabling it keeps the single-pass recommendation-only behavior.
- `review_fix_max_rounds`: maximum number of provisional Fix rounds, from `0` to `4`; this is not the total number of Review passes.
- `review_clean_confirmations`: consecutive issue-free whole-book Review passes required after shadow fixing, from `1` to `2`; the default is `2`.
- `review_autofix`: enabled by default. After the read-only Review engine finishes, publish its folded `changes` to a working translation, run the existing bounded Review Agent Loop once more over each remaining issue against that updated text, and pass confirmed issues to the existing Review Fixer. Pass `--no-autofix` or set this to `false` to keep Review from writing formal `target` values. The resulting complete segments replace only the formal chapter `target`; the manifest and glossary remain unchanged. Full before/after chains, issue IDs, decisions, failures, and write status are kept in the Review run's `autofix/index.json` instead of adding history fields to chapter JSON.
- `glossary_scope`: `chapter` includes terms relevant to the current chapter; `full` includes the complete glossary.
- `pdf_backend`: default `mineru` converts PDF via MinerU HTML. Use `babeldoc` for layout-preserving export through the external AGPL HTTP bridge.
- `babeldoc_bridge_url`: BabelDOC bridge base URL; default `http://127.0.0.1:8765`.
- `babeldoc_timeout`: HTTP timeout in seconds for bridge extract and fillback.
- `babeldoc_pages`: optional 1-based page selection such as `"15"` or `"6-8"`; omit it to process the whole file.

The command-line flags `--polish`, `--no-polish`, `--review`, and `--no-review`
override the corresponding configuration values for a `translate` run.

Run final review independently with `trans-novel review INPUT`. Each invocation
reviews the complete translated book from the beginning. By default, Review
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

- `mono`: produce a monolingual edition as `<book-name>.<target-language>.epub` (`.zh.epub` by default).
- `bilingual`: produce a source-and-translation edition as `<book-name>.<target-language>-bi.epub`.
- `bilingual_order`: `target_first` places the translation before the source; `source_first` reverses the order.
- `bilingual_preserve_source_style`: when `true`, source blocks inherit the book's normal text style instead of using the subdued gray style. This affects EPUB and HTML output only.
- `about_page`: append an “About this translation” project page to the book; set it to `false` to disable it.
- `punctuation_normalize`: normalize punctuation only on the in-memory export copy for Simplified Chinese targets. Traditional Chinese and other targets skip this deterministic conversion. Formal chapter `target` values, Review input, and resume state remain unchanged.

The former top-level `punctuation.normalize` key is not accepted; remove it and configure only `output.punctuation_normalize`.

Only the monolingual edition is enabled by default. `--bilingual` enables both editions, and configuration plus command-line switches can be combined to produce only the bilingual edition.

## Segmentation, honorifics, and paths

```yaml
segment:
  max_chars_per_batch: 1800
  max_chars_per_segment: 1200

honorific:
  strategy: keep_style

paths:
  state_dir: state
```

- `max_chars_per_batch`: approximate source-character budget for one model translation request.
- `max_chars_per_segment`: threshold for splitting an exceptionally long source paragraph.
- `honorific.strategy`: Japanese-source honorific policy: `keep_style`, `normalize`, or `drop`.
- `state_dir`: location of book checkpoints, chapter files, the glossary database, usage data, and reports. Subtitle runs store a separate tree at `<state_dir>/srt/<slug>/targets/<target-language>/` (manifest, cues, batches, usage, events) and never create a glossary or review directory.

All book targets, including the default `zh`, use `<state_dir>/<slug>/targets/<target-language>/`; subtitles use `<state_dir>/srt/<slug>/targets/<target-language>/`. Each directory owns its translations, glossary, context, accounting, and Review. Root-level state from earlier versions is no longer discovered or migrated. Start a new translation with the current configuration; existing files remain untouched. Saved manifests must include `source_lang`, `target_lang`, and a valid `source_sha256`.
