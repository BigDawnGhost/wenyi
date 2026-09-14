# Changelog

All notable changes to this project are documented here following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

- Added deterministic, model-free EPUB note relation detection and translation-slot protection for explicit semantics and conservatively paired short markers. The packaged Chinese reading theme now renders recognized Chinese-target references and backlinks as real solid circular `注` links while preserving note prose and the original link graph; no-theme, custom-theme and bilingual source labels remain unchanged, with reader-dependent navigation as the fallback.
- Added automatic source-bound migration for eligible schema-4 EPUB states whose slots differ only because note markers became protected. Existing translations are preserved without retranslation, current-policy fingerprints are exactly rebased, and a dedicated journal plus migration backup supports crash recovery. Unrelated slot changes, unlocatable old marker-only segments and recognized mixed-content note asides that cannot be extracted losslessly fail before writes.
- Replaced executable EPUB classification with data-only source-layout analysis through the analyst role. Original markup, context and relevant source CSS inform a closed semantic-role vocabulary; book-local class groups use sampling and held-out checks, with per-node expansion on conflicts and no unknown-to-body fallback.
- Layout classification now names JSON explicitly in its structured-output prompt, satisfying official provider JSON-mode admission without provider-specific handling.
- General EPUB styling now automatically analyzes missing or stale layout profiles, including completed-run assembly. Valid profiles and interrupted batches are reusable; `tools assemble --reanalyze-layout` explicitly refreshes without invalidating paid translations. Failed refreshes retain the prior accepted profile but stop the requested operation. Sampling can miss rare unseen exceptions, and conflicts can approach full-book analysis cost.
- Ordinary `translate` and `resume` now admit completed legacy-policy runs for layout, report and assembly maintenance without migrating policy or invalidating translated content. Incomplete, source-mismatched and future-policy runs remain rejected; TXT output remains layout-model-free.
- Removed `output.override_theme.rules` and the embedded classification runtime, assets and platform-specific build bootstrap. The strict object now accepts `styles` only; old keys fail validation. Presence-aware saved selections, config-relative CSS origins and explicit `override_theme: null` remain supported. Bilingual-only styling and TXT output require no layout analysis.
- Saved output selections now use schema version 2. Valid source-matching version-1 presentation snapshots are discarded with a notice and rebuilt from current CSS-only configuration or defaults, without migrating scripts or changing paid translations. Malformed snapshots and identity mismatches still fail; user YAML containing the removed `rules` key remains invalid.
- Bound source roles to actual rendered targets with original-source fingerprints rather than translated text or final DOM order. Added chapter-summary, quote-attribution and note roles while preserving note content, IDs, links, backlinks and structure. CSS/profile changes affect output fingerprints only; CSS-only edits reuse valid layout profiles.
- Retained exact-address CSS, protected-scope and inline-priority admission, independent theme reversal and preservation checks. All requested EPUBs verify before sequential durable publication; partial-success receipts and source/mono/bilingual triplet evidence remain bound to current file hashes and the output digest.
- Updated the offline frozen application smoke to exercise automatic layout analysis and cached reuse without provider requests. Earlier classifier test totals and reader screenshots do not establish acceptance of the data-only cutover.
- Fixed Windows usage persistence and EPUB publication when the CRT rejects directory handles. File `fsync` and atomic replacement remain mandatory; EPUB receipts explicitly report unsupported directory synchronization. POSIX permission failures still fail closed.
- Theme archive staging now uses the native filesystem basename, so Windows drive letters and directory separators cannot leak into temporary filenames. ZIP member paths remain POSIX paths.
- EPUB file synchronization now opens a non-truncating writable handle, as required by Windows, instead of suppressing file-sync errors.
- Polishing now sends one ID-addressed multi-paragraph request per existing chapter checkpoint, sharing one preceding source window and preserving per-segment quality checks and EPUB alignment.
- Exhausted polish protocol retries retain valid items from the final response and keep affected raw translations; ambiguous IDs reject the whole batch without per-segment rescue calls. Fallbacks are counted as rejected rather than accepted.
- New polishing calls use `polish.batch`, and completed batches record `checkpoint_batch_v1`; compatible runs keep completed results and resume pending checkpoints without a fingerprint or state-schema migration.
- New runs classify complete chapter source with the analyst role before style analysis and terminology seeding. Reference-only chapters preserve one original copy, including headings and navigation, in mono/bilingual output; large chapters use persisted source chunks, and mixed reference/prose chapters, uncertain observations, or conflicting chunk observations translate with review flags.
- Removed `--back-matter`, the `back_matter` setting, and the `skip/light/full` execution modes. Translation policy 2 requires a new run for incompatible model-driven work; completed legacy runs retain existing export eligibility without reclassification or migration.
- Rejected polishing proposals now require durable `events.jsonl` evidence before chapter/checkpoint commit, including full source and pre-polish/proposal text, checked text, lint details, request-time locked terms, locators, hashes, and configured editor candidates. Invalid or unavailable proposals remain null; this is decoded proposal evidence, not raw HTTP logging, and may contain private or copyrighted text.

## [1.3.0] - 2026-09-12
- EPUB archive resolution and link/backlink verification now use one shared external-scheme policy without changing the accepted protocols.
- EPUB backlink verification now shares explicit noteref semantics with text extraction, including ARIA references and EPUB namespace aliases, without guessing from filenames or attribute substrings.
- EPUB footnote exclusion now follows explicit noteref semantics only, preserving ordinary superscripts and text surrounding reference links. Resume and export reject incompatible saved source-slot layouts without migrating translations or overwriting existing outputs.
- EPUB verification now reopens known EPUB artifacts directly, including publication temporary files, and no longer discards unreadable or empty-content failures.
- Source-preserving EPUB verification now compares complete structural diagnostics by count against the input instead of allowlisting inherited error codes, accepting unchanged source-specific references while rejecting new or changed diagnostics.
- EPUB ingestion and verification now share package parsing and resource classification. Declared HTML media types take precedence over filenames; HTML suffix fallback applies only when media types are missing, and unsupported or unresolved spine entries fail explicitly instead of dropping chapters.
- EPUB rendering now updates tracked XHTML resources regardless of suffix, and NAV/NCX parsing follows package declarations rather than filename guesses while retaining both navigation sources.
- EPUB preflight verification now handles navigation XML comments and processing instructions, plus preserved footnote markers excluded from translation slots.
- Translation now advances rolling history within each batch and rebuilds chapter-entry history from saved preceding chapters instead of trusting stale context caches.
- Translation and optional polishing receive bounded preceding source paragraphs and the chapter title, with no future source window or additional model calls. Style guidance now distinguishes source evidence, book-level defaults, and natural Chinese expression.
- Runs record a translation policy version. Older runs retain their saved results and can export completed translations, but cannot resume model-driven work under the new policy; start a separate run instead.
- Removed the resolved translation-node class-size baseline exception.

## [1.2.0] - 2026-09-07
- Default translation now uses the OpenRouter `tencent/hy-mt2-30b-a3b` model alias.
- Usage accounting now persists each completed provider attempt synchronously through crash-safe schema-v2 WAL recovery, with real-time visibility and non-retryable local persistence failures.
- Moved local benchmark specifications, corpora, and run artifacts from the repository root into one ignored `benchmarks/` workspace.
- EPUB runs now dry-render and verify configured outputs before any model call or resumed publication, and publication checks accept XHTML 1.1 direct table columns and document-level footnote links.
- Indeterminate CLI phases now reset stale segment totals and elapsed time instead of appearing frozen after translation completes.

## [1.1.0] - 2026-09-05
- OpenRouter models can now constrain provider selection through per-model routing settings.
- Model-output protocol failures now use one shared bounded retry policy across analysis, translation, polishing, title translation, language detection, and prescan calls without restarting whole nodes; polishing no longer sends Arabic or Roman index page references to the model, and exhausted polish-item failures preserve the raw translation instead of discarding the batch.
- Resume fingerprints now use persisted run identity languages for translation, polishing, and title-translation nodes, preventing `auto`/detected-language drift from invalidating completed chapters.

## [1.0.2] - 2026-09-04
- Updated the default OpenCode Go Muse Spark Contributor model from 1.2 to 1.3 across runtime configuration, examples, and benchmark metadata.
- OpenCode Go requests now identify Wenyi and include a stable per-run `x-opencode-session` header.

## [1.0.1] - 2026-09-04
### Fixed

- Locked package metadata now matches release version 1.0.1.
- Repair now short-circuits the current pass after an LLM provider failure, preserving pending issues for the next resume while allowing report and EPUB assembly to continue.
- Fixed XML-incompatible characters in model-generated translations and titles before persistence or publication, including resumed legacy state.
- Source-preserving EPUB publication now accepts unchanged missing footnote backlinks inherited from the input while still rejecting new ones.
- State-backed bilingual EPUB verification now uses schema4 slot evidence and resolves persisted block paths independently of inserted source siblings.

## [1.0.0] - 2026-09-03
### Changed

- Reorganized pipeline, EPUB assembly, and benchmark packages and mirrored tests by capability; enforced their explicit dependency directions in the architecture gate.
- Deterministic QA now feeds one persisted issue-level Repair queue through the `editor` role, with an independent ten-call budget per issue, resumable ledger state, and guaranteed assembly after exhaustion.
- Reports expose Repair detected/resolved/exhausted counts and logical attempts, always set `requires_user_action: false`, and generate mono/bilingual outputs for exhausted issues.
- Heading segments in balanced/quality translation now use a concise plain-text prompt with source-matching glossary terms and the analyst role, while retaining strict retry/length validation.
- Balanced and quality translation now use one plain-text call per segment; empty or abnormally long responses retry and fail closed, while machine-readable literals bypass translation.
- Balanced/quality prose retries now fall back from an exhausted translator role to the analyst role without relaxing single-segment validation; headings and economy remain unchanged.
- Rich-text EPUB translations are now deterministically distributed by source slot length instead of asking the model to insert boundary markers.
- Targeted retranslation, polishing, and title translation now process one item per request to prevent content swaps in equal-length batch responses.
- EPUB output normalizes malformed source `mimetype` entries to the first uncompressed entry without a trailing newline and allows that metadata-only change through publication verification.
- The benchmark schema now supports minimal/polish clone ablations whose branches share immutable initial translations while recording separate target hashes and usage deltas.
- Added OpenRouter-catalog-verified metadata and a price snapshot for the pinned Tencent Hy-MT2 30B-A3B release, and stopped sending OpenRouter reasoning parameters to non-reasoning models.
- Model routing now exposes explicit `translator`, `analyst`, `editor`, and `fast` roles; Hy-MT2 30B handles plain-text translation by default while Muse Spark Contributor handles analysis, polishing, and fast tasks.
- EPUB translation now deterministically distributes plain-text output back to source slot boundaries.
- EPUB slot state now uses exact source/target values with complete whitespace-tail coverage; target distribution is lossless and schema versions require a fresh run.
- Benchmark candidate schemas now use complete four-role model IDs and compare minimal/polish arms for Hy-MT2 30B, GLM-5.3-Flash, and Muse Spark 1.2 Contributor.
- Benchmark validation now accepts independent translator, analyst, editor, and fast model IDs per candidate.
- Configuration examples now expose independent translator, analyst, editor, and fast model chains.

### Fixed

- The Chinese trillion unit now uses longest-token matching and normalizes to 10^12 without hiding genuine missing-number findings.

- EPUB verification marks each matched source block before resolving later duplicates.
- Reject review autofixes that introduce deterministic lint regressions or remove preserved dialogue quotes.
- Preserve non-linguistic segments without sending them to the LLM, including EPUB text-slot records.

- Direct-`br` bilingual proof now resolves nested inline owners structurally; machine-readable literals bypass polishing and retain exact EPUB slots.
- Added regression coverage for persisted Repair budgets and exhaustion behavior.

## [0.1.4] - 2026-08-28
### Added

- Added a public `editor` model role for independently configuring polishing and naturalization rewrites, inheriting `primary` when omitted.
- Added officially catalogued Alibaba Cloud Model Studio candidate capability metadata and constructor-injected controls for temperature and reasoning.
- Node input fingerprints now reflect the actual model-role combination so model changes invalidate only affected pipeline stages.
- Added optional telemetry for physical `LLM` calls with `JSONL` and collector sinks, plus immutable price snapshots and `Decimal`-based cost quoting.
- Added fully offline benchmark corpus scanning, manual selection, freezing, and validation commands with stable hashes, cross-book quota checks, and leakage detection.
- Added a production-equivalent chapter `EPUB` benchmark whose canary and full modes call `Application.run_all()` directly, isolate state and outputs per candidate and chapter, and require all six `formal` chapters for full runs.
- Added deterministic automated review that samples risk, dialogue, terminology, long sentences, and narrative; blinds every pair among two to six candidates as A/B; splits non-overlapping reviewer shards; and strictly validates coverage and verbatim evidence.
- Added automated review reports covering severity, error types, weighted errors per 10,000 words, per-book results, evidence, production state, and API cost from frozen price snapshots.
- EPUB source runs now persist schema-4 lxml text-slot contracts and write translations back to reopened source XHTML while preserving inline structure, resources, and vertical layout.
- EPUB exports now pass independent on-disk reopen verification, emit deterministic `epub_verification.json` reports, and publish atomically while preserving existing files and stable events on failure.
- Schema-4 bilingual EPUB publication now renders each lxml resource once using shared source cleanup, container/direct-`<br>` pairing, and bilingual style contracts; it supports `target_first` and `source_first` and rejects mapping or preserved-marker conflicts before publication.

### Changed

- The `balanced` and `quality` presets now always polish output; `economy` remains unpolished.
- Benchmark candidates must explicitly configure `editor_model`; attribution, shared preparation, the unpolished control, manual evaluation packs, post-edit timing, labor cost, and repricing paths were removed.
- The production benchmark provider moved from Alibaba Cloud Model Studio to OpenCode Go, comparing `deepseek-v4-flash`, `muse-spark-1.2-contributor`, and `mimo-v2.5` candidates that each perform translation and polishing, with per-request pricing from the official OpenCode Go rates.
- OpenAI Responses API models now reuse centralized request controls, response parsing, and token usage normalization.
- Removed the legacy template-based source EPUB fallback; source EPUB assembly now accepts only schema 4 slot state.

### Fixed

- Disabled built-in OpenAI SDK retries to avoid stacking them with centralized provider retries; malformed chat responses without `choices` retry as empty responses; strict polishing retries count mismatches with an exact-count constraint before falling back to individual segments; and full benchmarks reject formal EPUBs containing multiple logical chapters before any request.
- Fixed EPUB schema-4 slot insertion, bilingual source pairing, cross-slot punctuation normalization, and footnote marker recognition, with resume and review-statistics coverage for the strict pipeline.

## [0.1.3] - 2026-08-15

### Fixed

- Windows packaging smoke checks now return success after confirming that a missing input fails as expected, preventing the expected nonzero exit code from failing the release workflow.

## [0.1.2] - 2026-08-15

### Fixed

- Model language-detection failures now preserve the original exception so configuration errors such as missing API keys are reported directly.
- Body translation retries a whole batch and falls back per segment only for model protocol violations; provider and business errors no longer trigger outer duplicate calls or incorrect wrapping.
- Fixed multiplatform executable packaging, which still assumed `translate` created configuration automatically; smoke checks now run `init` explicitly to verify embedded defaults.

### Changed

- Event logs now use schema 2: routine translation, skip, polish, issue, and usage payloads retain only stable SHA-256 summaries and counts; rewrite audits retain before/after values and emit only after body persistence; append failures warn without blocking the pipeline; and redundant full-chapter writes were removed from translation, back matter, naturalization, and review flows.
- Development checks now enable additional Ruff quality rules, require absolute package imports, and reject new relative imports.

## [0.1.1] - 2026-08-15

### Added

- Code submissions must update `CHANGELOG.md`, enforced by the commit hook and CI.
- Pushing a version tag now creates a GitHub Release using that version's changelog entry as release notes.
- Added Alibaba Cloud Model Studio provider support and reasoning controls for DeepSeek V4 Flash and Qwen 3.7 Flash.

### Changed

- Configuration was reduced to `llm.provider`, `llm.models.primary`, `llm.models.fast`, and `quality`; legacy provider directories, agent routing, and pipeline tuning formats are no longer accepted.
- Missing `config.yaml` now uses built-in OpenCode Go, DeepSeek V4 Flash, and `balanced` defaults without creating a file.
- Added per-run `--quality`, `--source-language`, `--back-matter`, and `--honorifics` overrides.
- The default `primary` and `fast` roles now both use OpenCode Go `deepseek-v4-flash` to reduce long-form translation cost.
- Model specifications now accept an `:<thinking-level>` suffix with `off`, `low`, `medium`, `high`, or `max`; startup validates each level against provider and model capabilities, with OpenCode Go `deepseek-v4-flash:high` for `primary` and `:off` for `fast` by default.
