# Configuration

[简体中文](zh/configuration.md)

Wenyi reads `config.yaml` from the current working directory. If the file is missing, running the program creates a documented default configuration.

## Languages

```yaml
language:
  source: auto
  target: zh
```

`source: auto` asks the model to identify the source language. You may instead use an ISO 639-1 code such as `ja`, `en`, `ko`, `ru`, `fr`, `de`, or `es`. The current translation pipeline is primarily designed for Simplified Chinese output.

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

The first PDF import also reads `MINERU_API_KEY` to call the MinerU conversion service. This key is independent of the LLM provider and is not written to `config.yaml`.

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
  review: false
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
  glossary_scope: chapter
```

- `review`: disabled by default; when enabled, automatically run the evidence-driven whole-book review after the complete book has been translated. The explicit `trans-novel review` command remains available while this is disabled.
- `polish`: run the strong model over translated batches again for style. This may improve quality but significantly increases runtime and cost.
- `rolling_context_segments`: number of recent translated segments included with each translation batch.
- `book_understanding`: prescan the book to create chapter digests and a whole-book synopsis.
- `prescan_concurrency`: number of chapter-digest requests that may run concurrently.
- `annotation_alignment`: enabled by default. After each annotated logical paragraph has been fully translated and polished, finalize its punctuation and immediately locate EPUB footnote/endnote links with one sequential model call. Split continuations are rejoined first, and segments without internal links do not call the model. When disabled, translated links remain clickable but fall back to end-of-paragraph markers; untranslated text and the source side of bilingual output retain the original link positions. This option controls link placement only; resolved source-language note content is supplied to translation automatically.
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
- `glossary_scope`: `chapter` includes terms relevant to the current chapter; `full` includes the complete glossary.

The command-line flags `--polish`, `--no-polish`, `--review`, and `--no-review`
override the corresponding configuration values for a `translate` run.

Run final review independently with `trans-novel review INPUT`. Each invocation
reviews the complete translated book from the beginning. Review may modify only a
run-local shadow translation; it never persists replacements to formal translation
state. The consolidated result and internal round records are written under
`state/<book>/reviews/review-<timestamp>/`. Review usage is stored both as the
run-local delta and in the book's cumulative usage totals.

## Output

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  bilingual_preserve_source_style: false
  about_page: true
```

- `mono`: produce the monolingual Chinese edition as `<book-name>.zh.epub`.
- `bilingual`: produce a source-and-translation edition as `<book-name>.zh-bi.epub`.
- `bilingual_order`: `target_first` places the translation before the source; `source_first` reverses the order.
- `bilingual_preserve_source_style`: when `true`, source blocks inherit the book's normal text style instead of using the subdued gray style. This affects EPUB and HTML output only.
- `about_page`: append an “About this translation” project page to the book; set it to `false` to disable it.

Only the monolingual edition is enabled by default. `--bilingual` enables both editions, and configuration plus command-line switches can be combined to produce only the bilingual edition.

## Segmentation, honorifics, punctuation, and paths

```yaml
segment:
  max_chars_per_batch: 1800
  max_chars_per_segment: 1200

honorific:
  strategy: keep_style

punctuation:
  normalize: true

paths:
  state_dir: state
```

- `max_chars_per_batch`: approximate source-character budget for one model translation request.
- `max_chars_per_segment`: threshold for splitting an exceptionally long source paragraph.
- `honorific.strategy`: Japanese-source honorific policy: `keep_style`, `normalize`, or `drop`.
- `punctuation.normalize`: normalize output to common full-width Simplified Chinese punctuation.
- `state_dir`: location of book checkpoints, chapter files, the glossary database, usage data, and reports. Subtitle runs store a separate tree at `<state_dir>/srt/<slug>/` (manifest, cues, batches, usage, events) and never create a glossary or review directory.
