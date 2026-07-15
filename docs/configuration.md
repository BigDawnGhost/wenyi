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
        thinking: false
```

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
  autofix_severe: false
  polish: true
  backtranslate_sample: 0
  consistency_qa: false
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  review_concurrency: 4
  glossary_scope: chapter
```

- `review`: check each completed chapter for omissions, mistranslations, terminology, and incorrect references.
- `autofix_severe`: retranslate severe omissions and mistranslations and adopt fixes that pass validation.
- `polish`: run the strong model over translated batches again for style. This may improve quality but significantly increases runtime and cost.
- `backtranslate_sample`: fraction of translated segments to inspect through backtranslation; `0` disables it.
- `consistency_qa`: run a final cross-chapter check of terminology, references, voice, and punctuation.
- `rolling_context_segments`: number of recent translated segments included with each translation batch.
- `book_understanding`: prescan the book to create chapter digests and a whole-book synopsis.
- `prescan_concurrency`: number of chapter-digest requests that may run concurrently.
- `review_concurrency`: number of chapter-review chunks that may run concurrently; set it to `1` for sequential review.
- `glossary_scope`: `chapter` includes terms relevant to the current chapter; `full` includes the complete glossary.

The command-line flags `--polish`, `--no-polish`, `--qa`, and `--no-qa` override the corresponding configuration values for that run.

## Output

```yaml
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  about_page: true
```

- `mono`: produce the monolingual Chinese edition as `<book-name>.zh.epub`.
- `bilingual`: produce a source-and-translation edition as `<book-name>.zh-bi.epub`.
- `bilingual_order`: `target_first` places the translation before the source; `source_first` reverses the order.
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
- `state_dir`: location of checkpoints, chapter files, the glossary database, usage data, and reports.
