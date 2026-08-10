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

## LLM API

Wenyi uses one universal client for two text protocols: Anthropic Messages and
OpenAI Chat Completions. A real model run needs four pieces of information:

```yaml
llm:
  # anthropic | openai; the case-insensitive aliases a | oai also work
  api_format: openai

  # Choose either source. If both are present, api_key takes precedence.
  api_key_env: LLM_API_KEY
  # api_key: sk-...

  # An SDK base URL or a complete operation URL is accepted.
  base_url: https://api.example.com/v1/chat/completions
  model: provider-model-name
```

`api_key` is stored as a secret value and is redacted from configuration
representations. An environment variable is still recommended so that a key is
not committed accidentally. `api_format: fake` is reserved for offline tests and
does not make network requests.

The first PDF import separately reads `MINERU_API_KEY` for the MinerU conversion
service. That key is unrelated to the LLM API configuration.

Real-client validation is deferred until a command actually needs a model. Thus
commands such as `--help`, `assemble`, and `report` remain usable while the
generated `base_url` and `model` placeholders are still empty. A model workflow
reports all missing required fields before its first request.

### Optional request settings and model tiers

All other LLM fields are optional. Global values apply to `strong`, `cheap`, and
`fast`; a tier overrides only the fields it declares:

```yaml
llm:
  api_format: openai
  api_key_env: LLM_API_KEY
  base_url: https://api.example.com/v1
  model: provider-model-name

  timeout: 600
  max_retries: 4
  max_tokens: 8192
  max_tokens_field: max_tokens # max_tokens | max_completion_tokens (OpenAI only)
  temperature: 0.2
  thinking: true
  reasoning_effort: high
  request_overrides:
    metadata:
      application: wenyi

  tiers:
    fast:
      model: provider-fast-model
      thinking: false
      request_overrides:
        metadata:
          workload: prescan
```

There is no cross-tier model fallback. In this example, `strong` and `cheap` use
the global model, while `fast` uses its exact override. Only `strong`, `cheap`,
and `fast` are valid tier names; misspelled or unknown tiers fail explicitly
instead of silently selecting the global model. Global and tier
`request_overrides` are merged recursively, so both `metadata.application` and
`metadata.workload` reach the fast request.

`request_overrides` is the escape hatch for relay- or vendor-specific raw request
fields. It cannot replace client-owned structure such as `model`, `messages`,
`stream`, credentials, token-limit fields, or Anthropic's top-level `system`.
Explicit call arguments, JSON mode, and a caller-provided `max_tokens` take final
precedence.

`max_retries` is the number of additional attempts managed by Wenyi. Both SDKs
have their own retries disabled. Wenyi retries transient connection and timeout
errors, HTTP 408/409/429, and 5xx responses; retry waits and request activity are
recorded in the book's `events.jsonl`. Valid server `Retry-After` and
`retry-after-ms` values are honored in full; only Wenyi's fallback exponential
backoff is capped at 30 seconds.

### Base URL normalization

`base_url` must use HTTP or HTTPS and cannot contain a query or fragment. Wenyi
accepts either an SDK base address or a complete standard operation address:

- OpenAI format strips a trailing `/chat/completions` before passing the URL to
  the SDK.
- Anthropic format strips a trailing `/v1/messages`.
- Other custom path prefixes are preserved exactly, apart from trailing slashes.

For example, both `https://api.example.com/v1` and
`https://api.example.com/v1/chat/completions` select the same OpenAI endpoint.

### OpenAI Chat Completions format

The OpenAI branch preserves system, user, and assistant messages and calls
`chat.completions.create`. JSON mode adds a JSON instruction to the prompt and
sends `response_format: {type: json_object}`. `thinking: true` sends the selected
`reasoning_effort` (defaulting to `high`), while `thinking: false` sends `none`.

The default output-limit field is the widely compatible `max_tokens`. Set
`max_tokens_field: max_completion_tokens` when an endpoint or newer OpenAI model
requires that spelling.

Common OpenAI-format examples are:

```yaml
# OpenAI
llm:
  api_format: openai
  api_key_env: OPENAI_API_KEY
  base_url: https://api.openai.com/v1
  model: your-openai-model

# Google Gemini through its OpenAI-compatible endpoint
llm:
  api_format: openai
  api_key_env: GEMINI_API_KEY
  base_url: https://generativelanguage.googleapis.com/v1beta/openai
  model: your-gemini-model

# OpenRouter (DeepSeek and other relays use the same shape)
llm:
  api_format: openai
  api_key_env: OPENROUTER_API_KEY
  base_url: https://openrouter.ai/api/v1
  model: provider/model-name

# Local Ollama; use any non-empty key if the server does not authenticate
llm:
  api_format: openai
  api_key: local
  base_url: http://localhost:11434/v1
  model: installed-model-name
```

A typical local vLLM address is `http://localhost:8000/v1`; DeepSeek's API base
is `https://api.deepseek.com`. Wenyi no longer supplies vendor URLs, model names,
or environment-variable names automatically.

### Anthropic Messages format

The Anthropic branch moves all system-message content to the top-level `system`
field and keeps the remaining user/assistant sequence. JSON mode constrains the
prompt without sending the OpenAI-only `response_format` field. If no output
limit is configured, Anthropic requests use `max_tokens: 8192`.

`thinking: true` selects adaptive thinking and `thinking: false` selects disabled
thinking. `reasoning_effort` maps to `output_config.effort`. For an older model
that requires a fixed thinking budget, replace the generated thinking object with
a complete vendor-specific value:

```yaml
llm:
  api_format: anthropic
  api_key_env: ANTHROPIC_API_KEY
  base_url: https://api.anthropic.com
  model: your-claude-model
  request_overrides:
    thinking:
      type: enabled
      budget_tokens: 8192
```

Only final text blocks are returned to the translation pipeline; thinking and
tool blocks are ignored. Anthropic cache creation/read tokens and OpenAI cached
prompt tokens are normalized into Wenyi's existing usage statistics.

### Migrating old provider configuration

By default Wenyi trusts only the standard `content` response field and retries
an empty response. For an OpenAI-format endpoint known to place the final JSON
answer in `reasoning_content`, set `json_response_fallback: reasoning_content`
globally or on the applicable tier. The fallback is read only in JSON mode and
accepted only when the entire field is one valid JSON value.

```yaml
llm:
  api_format: openai
  base_url: https://api.example.com/v1
  model: provider-model-name
  tiers:
    strong:
      json_response_fallback: reasoning_content
```

The former `llm.provider`, `reasoning_style`, and `tiers.*.options` fields are not
compatible aliases. Wenyi rejects them with a migration example. Replace them
with `api_format`, an explicit `base_url`, a global `model`, and the flat optional
fields shown above. Put only true provider extensions under `request_overrides`.

## Pipeline

```yaml
pipeline:
  review: false
  polish: true
  backtranslate_sample: 0
  rolling_context_segments: 6
  book_understanding: true
  prescan_concurrency: 4
  annotation_alignment: true
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
- `backtranslate_sample`: fraction of translated segments to inspect through backtranslation; `0` disables it.
- `rolling_context_segments`: number of recent translated segments included with each translation batch.
- `book_understanding`: prescan the book to create chapter digests and a whole-book synopsis.
- `prescan_concurrency`: number of chapter-digest requests that may run concurrently.
- `annotation_alignment`: enabled by default. After each annotated logical paragraph has been fully translated and polished, finalize its punctuation and immediately locate EPUB footnote/endnote links with one sequential model call. Split continuations are rejoined first, and segments without internal links do not call the model. When disabled, translated links remain clickable but fall back to end-of-paragraph markers; untranslated text and the source side of bilingual output retain the original link positions. This option controls link placement only; resolved source-language note content is supplied to translation automatically.
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
- `state_dir`: location of checkpoints, chapter files, the glossary database, usage data, and reports.
