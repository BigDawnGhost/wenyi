# Configuration

[简体中文](zh/configuration.md) · [CLI usage](usage.md) · [Web deployment](web.md)

Both interfaces use `wenyi_core.config.Config`. CLI reads `--config` (default `config.yaml`); Web reads server defaults from `WENYI_CONFIG`, applies the selected workflow preset and then the project's saved overrides. Web paths are managed by the server. Provider credentials remain in environment variables.

## Minimal configuration and defaults

```yaml
language:
  source: auto
  target: zh
llm:
  preset: deepseek
segment:
  max_tokens_per_batch: 1800
  max_tokens_per_segment: 1200
pipeline:
  book_understanding: true
  polish: true
  review: true
  review_autofix: true
output:
  mono: true
  bilingual: false
  punctuation_normalize: true
```

Run `trans-novel languages` for supported language codes. Metadata produced by the model follows the target language; the Web interface remains Chinese. Target language and source identity cannot change after a Web project is initialized.

Token budgets use `tiktoken`'s `cl100k_base` encoding. They control source grouping and sentence-boundary splitting, not provider output limits. `max_tokens_per_segment` replaces the old character-based segment setting.

## Model providers, profiles and routes

The registry includes DeepSeek, OpenAI, OpenRouter, OrcaRouter, Google Gemini, Ollama, vLLM, generic OpenAI-compatible endpoints and an offline Fake provider for tests. The preset supplies `providers`, `models` and `tiers`; an explicit configuration may define all of them instead. Tier names are exactly `strong`, `cheap` and `fast`.

Example: keep the DeepSeek preset and route review scanning to a second provider:

```yaml
llm:
  preset: deepseek
  providers:
    review_connection:
      kind: openai
      api_key_env: OPENAI_API_KEY
      timeout: 600
      max_retries: 4
      max_concurrency: 2
  models:
    review_model:
      provider: review_connection
      model: your-review-model
      max_output_tokens: 4096
  routes:
    review.scan:
      model: review_model
      fallbacks: [default_cheap]
```

Replace `your-review-model` with an actual model supported by the selected endpoint. No credentials are embedded in YAML. Each route selects exactly one `model` or `tier`; fallbacks are an explicit ordered list of model profile names. Provider-specific request options belong in `models.<name>.options` and are validated by that provider's adapter.

Provider/profile definitions supplied over a preset replace the definition with the same name; include the complete connection/profile when overriding one. Project overrides merge configuration sections and named maps. Selecting a different preset replaces the old expanded model configuration with the new preset plus its explicit overrides.

```bash
uv run trans-novel models list
uv run trans-novel models explain --operation translation.body
uv run trans-novel models check --workflow review
```

Common operations include `language.detect`, `analysis.style`, `synopsis.chapter`, `synopsis.book`, `translation.body`, `translation.title`, `polish.body`, `glossary.extract`, `annotation.align`, `review.scan`, `review.verify`, `autofix.verify`, `autofix.fix` and `srt.translate`. The registry and `/capabilities` are the authoritative list.

### Limits and budgets

Connections support `max_concurrency`, `max_retries`, `timeout` and an optional `quota_group`. Define group limits in `llm.quotas` and per-invocation budgets in `llm.budget`:

```yaml
llm:
  preset: deepseek
  budget:
    max_requests: 2000
    max_tokens: 2000000
    deadline_seconds: 7200
```

Quota groups can specify `requests_per_minute` and `tokens_per_minute`. These limits are maintained in the current client process; they are not a distributed account-wide limiter across several Worker processes. Explicit fallback routes, provider retries and all successful usage are included in the operation records.

## Book workflow options

| Setting | Default | Meaning |
|---|---:|---|
| `pipeline.book_understanding` | `true` | Chapter digests and whole-book synopsis before body translation. |
| `pipeline.polish` | `true` | Polish translations and retain the pre-polish text. |
| `pipeline.review` | `true` | Run whole-book Review after translation. |
| `pipeline.review_autofix` | `true` | Publish accepted Review revisions to formal targets. |
| `pipeline.rolling_context_segments` | `6` | Recent translated paragraphs supplied as context. |
| `pipeline.prescan_concurrency` | `4` | Independent chapter-digest workers. |
| `pipeline.annotation_alignment` | `true` | Place EPUB annotation links within translated paragraphs. |
| `pipeline.annotation_alignment_concurrency` | `4` | Concurrent per-annotation alignment requests. |
| `pipeline.align_retry_limit` | `2` | Translation alignment retries before smaller fallback work. |
| `pipeline.glossary_scope` | `chapter` | Terms relevant to a chapter; `full` supplies the whole glossary. |

Web's standard workflow uses the server defaults. The quick preset disables understanding, polishing, Review and Autofix. Custom steps or project YAML can override these choices. A running project rejects conflicting configuration and manual content edits.

### Review options

| Setting | Default |
|---|---:|
| `review_concurrency` | `4` |
| `review_output_retries` | `2` |
| `review_agent_loop` | `true` |
| `review_agent_max_evidence_rounds` | `2` |
| `review_conflict_arbitration` | `true` |
| `review_fix_loop` | `true` |
| `review_fix_max_rounds` | `2` |
| `review_clean_confirmations` | `2` |

All rows belong under `pipeline`. The fix loop edits a shadow translation; `review_autofix` controls the separate publication stage. Use `--no-autofix` or `review_autofix: false` for recommendations only. Matching completed reviews are reused, and matching interrupted runs resume; a change to relevant routes/configuration, terms or translated content invalidates reuse.

## Output, PDF and paths

```yaml
pipeline:
  pdf_backend: mineru
  babeldoc_bridge_url: http://127.0.0.1:8765
  babeldoc_timeout: 600
  # babeldoc_pages: "6-8"
honorific:
  strategy: keep_style
paths:
  state_dir: state
output:
  mono: true
  bilingual: false
  bilingual_order: target_first
  bilingual_preserve_source_style: false
  about_page: true
  punctuation_normalize: true
```

`honorific.strategy` accepts `keep_style`, `normalize` or `drop`. Source-specific guidance is used where available. Bilingual order accepts `target_first` or `source_first`.

`punctuation_normalize` acts only on exported copies for the applicable target language, preserving saved model text. `paths.state_dir` is a CLI setting; Web rejects project-controlled storage paths.

MinerU is the default PDF parser and uses `MINERU_API_KEY`. BabelDOC is an optional independently deployed HTTP bridge selected with `pdf_backend: babeldoc`. In containers, use a bridge hostname reachable from the API and both workers; `127.0.0.1` refers to each container itself. The backend recorded during parsing determines whether a later PDF export uses BabelDOC backfill.

For fpdf2 output, fonts must contain **TrueType outlines** (a `glyf` table). The exporter skips incompatible OpenType/CFF candidates and rejects an explicitly configured incompatible font before writing a PDF. Install `fonts-wqy-zenhei` or set `TRANS_NOVEL_PDF_FONT` to a compatible TTF/TTC font covering the source and target characters. Docker includes WenQuanYi Zen Hei for fpdf2; WeasyPrint continues to use the installed Noto CJK fonts. A font's file extension alone does not identify its outline format.

## Removed settings

Independent consistency QA, backtranslation sampling, `autofix_severe`, `review_agent_tier`, the old `force` parameter and character-budget fields have been removed. Punctuation normalization moved to `output`. Configuration validation rejects unsupported pipeline/segment fields. This Web update uses a fresh schema and has no automatic migration of old Web strategies or databases.
