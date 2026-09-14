# EPUB Source Layout Analysis and Theme Design

Status: approved data-only cutover contract. Acceptance requirements below are not claims of completed validation.
Date: 2026-09-13.

## 1. Decisions and scope

Enable general EPUB typography with `output.override_theme.styles`. The host analyzes **original source content**, saves semantic roles as data, and applies user-selected CSS to the corresponding output elements. Classification and appearance are separate: changing CSS does not require another layout analysis or translation.

The analyst model returns roles for host-issued IDs, not executable code, selectors, CSS, paths, or DOM mutations. Original markup, adjacent source context, ancestor features, relevant original CSS, and internal reference relationships are evidence. Translated text and selected output CSS are not classifier input. Source content is untrusted evidence, not instructions that can change the response contract.

Class names are book-local grouping evidence, never universal semantics. A class used for quotations in one book may denote chapter summaries in another; even within a book, one class may serve multiple purposes. There is no book-title/ISBN dispatch, publisher-specific role table, or unconditional paragraph-to-body fallback.

The host continues to own text, source/translation pairing, DOM integrity, archive I/O, validation, and publication. CSS owns appearance. Neither model output nor configuration can disable preservation checks. There is no executable theme plugin or embedded classification runtime. Fonts are not bundled; CSS declares local families and fallbacks.

EPUB note protection is a neutral, model-free source pass. Explicit EPUB/ARIA noteref, backlink, footnote and endnote semantics are authoritative when their targets resolve. Untyped inference is deliberately narrower: the visible anchor must be a short supported symbol or ASCII number, the return marker must begin its own note block, both links must resolve safely and reciprocally, and numeric pairs additionally require source note semantics from XHTML or exact OPF/NAV metadata. Duplicate, missing, external, unsafe or directionally ambiguous targets are omitted rather than guessed. Classes, IDs, filenames and arbitrary words never establish note meaning. Explicit malformed markers remain protected from translation, but do not create invented relation records.

## 2. Configuration and reassembly

Replace the existing `output` mapping to enable the packaged reading style:

```yaml
output:
  mono: true
  bilingual:
    enabled: true
    order: target_first
  override_theme:
    styles: builtin:chinese-reading
  bilingual_styles: builtin:bilingual
```

Custom CSS uses the same fields:

```yaml
output:
  override_theme:
    styles: ./themes/chinese-reading.css
  bilingual_styles: ./themes/bilingual.css
```

- `override_theme` defaults to `null`: no general typography override and no layout-model calls. Bilingual-only presentation remains available without a layout profile or model analysis.
- The non-null object requires `styles` only. The removed `rules` key is rejected, not ignored or treated as a compatibility alias. Remove it from existing configuration.
- `bilingual.order` accepts `target_first` or `source_first`; defaults remain mono and bilingual enabled with `target_first`. Disabling both variants normalizes to mono-only.
- Precedence is existing explicit CLI output flags, explicit current config fields, saved output selections, then defaults. Omission preserves saved choices; explicit `override_theme: null` clears the saved general theme.

- Saved output selections now use schema version 2. On first use of this version, a structurally valid, source-matching version-1 presentation snapshot is discarded with `output_selection_obsolete`; its old script/style selections are not migrated. Current CSS-only configuration or defaults determine the replacement, so provide the new configuration above to select general styling. With that styling enabled, the obsolete cache does not block automatic missing-profile analysis. Explicit `override_theme: null` remains model-free. Malformed snapshots, unknown versions and source mismatches still fail. Only the old presentation cache is discarded, never paid translations; user YAML containing `rules` still fails validation.
- Custom paths resolve relative to the selected config file, with `~` expansion. `Config.from_dict(raw, *, base_dir=None)` requires a base directory for relative custom paths. Saved paths and origins are provenance, not semantic digest inputs.
- Unknown keys, invalid types, empty paths and unsupported built-in identifiers fail validation. A missing saved CSS file fails rather than silently selecting a default.
- A custom bilingual stylesheet replaces the packaged file; an intentionally empty stylesheet is valid.
- TXT output retains bilingual order and saved EPUB choices, but neither reads presentation assets nor runs layout analysis. Missing unused CSS must not break TXT export.

The primary workflow remains `translate`; use `resume` after interruption. From the same
working directory and saved run state:

```bash
trans-novel --config config.yaml translate book.epub
trans-novel --config config.yaml resume book.epub
```

Completed legacy-policy runs use these ordinary entry points too. The application does not
migrate the saved policy or rerun the paid content chain; it runs layout, report and assembly
only. A legacy run with incomplete required content, a mismatched source identity or incompatible
EPUB source slots is still rejected before any model call. Future policy versions are never
admitted as legacy runs.

For an explicit export-only operation, `tools assemble` remains available:

```bash
trans-novel --config config.yaml tools assemble book.epub --format epub
```

**These output-maintenance paths do not call translation models, but general styling may call
the analyst model and incur cost.** Missing or stale layout data is analyzed automatically,
including completed legacy runs without a layout profile; a valid profile is reused. TXT output
does not read theme assets or run layout analysis. To explicitly refresh EPUB layout data:

```bash
trans-novel --config config.yaml tools assemble book.epub --format epub --reanalyze-layout
```

Refresh is output maintenance, not a translation reset. It requires the same source identity as
the saved run. A failed refresh stops the requested operation and leaves the prior accepted
profile intact; it does not silently publish with that profile. For source-backed EPUB, missing
or changed original source cannot be replaced with reconstructed translated content.

## 3. Source inventory and classification

### Eligible source evidence

`build_layout_inventory(source_path, chapters)` reads original EPUB archive resources through the existing safe archive, markup projection and source stylesheet readers. Generated EPUBs use the persisted original column from `merged_paragraphs`, not a second paragraph-merging algorithm.

Eligible content includes h1–h6, p, li, blockquote, td, th, dt and dd; explicit caption, subtitle, note-reference and note semantics; and containers with direct visible text. Structural ancestors are not classified merely because descendants have text. Protected resources/subtrees and preserved source scopes remain outside application. Package-declared cover/title-page and fixed-layout resources are excluded before CSS/model evidence collection, so preserved resources do not incur layout-analysis cost or fail because of unused source CSS.

A group combines tag, sorted classes, EPUB types, ARIA role, parent tag/classes and the original stylesheet-family digest. For EPUB, its identity also includes the deterministic matched original CSS evidence for the node and its ancestors, including inline styles. This separates nodes whose applicable source styles differ through `:nth-child`, ancestor selectors or inline declarations even when their classes and stylesheet family match. Adjacent blocks provide context without expanding group identity. Relevant selectors, declarations and inline styles are evidence, not browser-computed layout; selected output CSS is never part of grouping. Unsupported original CSS follows the existing explicit source-CSS error policy.

Adjacent context is a bounded preview of at most 1,000 characters with an explicit truncation indicator. This differs from the classified node's own source markup, which is retained in full and chunked when necessary.

### Sampling, uncertainty and cost

Groups of at most six nodes are analyzed in full. Larger groups use deterministic first/middle/last representatives, spreading resources where possible, followed by up to three other distinct held-out samples. A shared non-null role and heading level across all observations permits propagation to the remaining group members.

If samples conflict or contain unknown results, analyze the remaining members individually and reuse observations already obtained. Every inventory node appears in the final profile, including unknown nodes. Conflicts can therefore approach a full-book pass and cost substantially more than initial sampling. Sampling is not a proof of uniformity: a rare exception outside the observed samples can still be missed. Explicit refresh repeats the analysis; it is not a guarantee that such an exception will be discovered.

Requests contain at most 16 observations and 24,000 serialized request characters. Long source evidence is split into complete chunks of at most 8,000 characters with stable chunk IDs and bounded repeated context. A node receives a role only if all chunks agree on the same non-null role and level; otherwise it remains unknown. The classified node's own evidence is not silently reduced to a text prefix. Structural/CSS context that cannot fit fails explicitly with sanitized `layout_context_limit`.

`LayoutAnalyzer.classify(*, samples, allowed_roles)` uses the `analyst` model role and operation `layout.classify`. It reuses existing protocol retry and usage accounting. The JSON envelope is `{"observations": [...]}`; each requested ID must occur exactly once. Missing, duplicate or foreign IDs, unknown keys, invalid roles and invalid heading levels fail protocol validation. There is no parallel model-call layer or separate retry policy for layout.

### Role contract and unknown behavior

The closed role vocabulary is:

- `body`, `heading`, `chapter-number`, `subtitle`, `caption`;
- `quote`, `quote-text`, `quote-attribution`, `chapter-summary`;
- `table-text`, `list-text`, `footnote`, `endnote`, `noteref`.

`heading` requires an integer level from 1 through 6. All other roles and unknown (`null`) require no level. Native heading, quote and note semantics are strong evidence, not a hidden second classifier.

Unknown means no explicit role assignment; it never means body. General theme CSS is admitted only on elements with an accepted non-null semantic role. Even a custom `p` or `*` selector cannot directly style an unknown target through the general theme. Role markers belong only to explicitly classified elements; an unknown child does not receive an ancestor's role marker. Bilingual source styling remains independent of this general-theme admission boundary. Ordinary CSS inheritance still applies: an unknown child can inherit font or color from a themed ancestor, so unknown is not a promise of visual isolation. Role data does not rename tags, change TOC levels, reorder content, or alter translation segmentation.

## 4. Persistence and sequencing

The run stores `layout_work.json` for resumable analysis and `layout_profile.json` for the accepted result using existing `RunStore` locking and atomic JSON writes. Successful bounded batches survive interruption. Work checkpoints include inventory/policy identity and the analyst configuration fingerprint; work from another model configuration is not mixed into the resumed analysis.

Accepted-profile validity depends on original source, inventory and layout policy, **not** selected CSS or analyst configuration. Changing the analyst selection does not discard a valid completed profile; use `--reanalyze-layout` to request a fresh one. Provenance such as usage or timestamps does not affect the profile's semantic digest.

The strict profile schema contains `schema_version=1`, `source_sha256`, `inventory_digest`, `policy_version`, `assignments` and JSON-only object `provenance`. Each assignment has host-owned node ID, resource path, element path and source fingerprint plus validated role/level. Unknown fields, malformed digests, duplicate IDs/locators, boolean path indices and invalid roles/levels are rejected. Persisted data is not trusted merely because it is local.

Normal full EPUB execution runs layout after preparation has initialized the manifest and before body translation. Static source/CSS/archive checks are model-free; exact role-dependent output preflight follows layout analysis and precedes body translation. Existing preparation can itself call models, so this is not a promise that every failure is detected before any paid work.

The book-level `layout` node depends on `prepare`; assembly depends on `report` and `layout`. Runtime sequencing does not introduce a content dependency from layout to analysis, terminology or translation. When general EPUB styling is inactive, layout is explicitly skipped rather than represented by a fake successful profile.

The output digest includes CSS bytes, effective output choices, compiler policy and the semantic layout digest. Moving identical CSS to another path does not change it. CSS/profile changes invalidate assembly and output verification only, never already-paid translated chapters. Usage and output records can change during reassembly even when translation state does not. Progress distinguishes analysis from saved-result reuse.

For a source-matching schema-4 EPUB state created before deterministic marker protection, eligible `translate`, `resume` and output-only assembly paths run note-slot compatibility before publication work. Migration accepts only differences independently attributable to newly protected marker subtrees and slot reindexing, preserves existing translated payloads without translation calls, and rebases already-succeeded current-policy fingerprints only from their exact saved pre-migration values. The complete result is validated before the first chapter write. A dedicated source-bound `note_migration.json` journal permits idempotent forward recovery, while `note_migration_backup/` retains the verified pre-migration data; the note metadata is committed last, independently of translation and polish journals. Configured languages are normalized before identity checks: `auto` reuses the saved language, while an explicit mismatch fails before mutation. Identity, policy, unrelated slot, payload-attribution or fingerprint conflicts also fail before mutation.

Two extraction boundaries intentionally fail closed. An old marker-only segment cannot be deleted without a structural locator, so that legacy state is rejected instead of receiving a fake empty slot contract. A newly recognized `aside` is extracted as a leaf only when existing nested block extraction would not cover it; if nested blocks coexist with non-marker direct prose that would be dropped, ingestion rejects the mixed-content container rather than silently losing text. Ordinary `aside` elements are not globally promoted into translation blocks.

## 5. Source-to-output binding and bilingual content

The source renderer records eligible original elements, paths and fingerprints before mutation and resolves surviving target element references afterward. Direct-text runs reuse explicit source-pair mappings. Generated EPUBs bind original merged-paragraph indices to actual target elements in `ch{chapter.index}.xhtml`. Neither renderer guesses correspondence from final DOM order or translated text.

`ResourceThemeScope.layout_bindings` carries `LayoutBinding(source_path, target_paths, source_sha256)`. `ThemeService` looks up profile entries by original resource/path and validates the original fingerprint against these bindings. The global source digest and per-node original-content digest are distinct. Missing mappings for eligible non-null assignments fail explicitly; unused entries in fully preserved scopes are allowed only with verified preservation.

Bilingual source copies are not analyzed again. They receive corresponding target role/level only where pairing is unambiguous. A combined source wrapper with different target roles receives only its source-content marker unless individual descendants can be safely mapped. Original/translation order remains actual DOM order, controlled by output configuration.

Deterministic note mappings follow the same original-to-target binding. In bilingual output, only the translated target marker is eligible for the packaged marker treatment; the source copy retains its original label, attributes and link graph and is never remapped as a target. No-theme and custom general styles also retain original marker labels.

Permanent presentation markers are:

- `data-tn-role` and, for headings, `data-tn-level`;
- `data-tn-content="target"` or `data-tn-content="source"`;
- `data-tn-note-kind="noteref"` or `"backlink"` on admitted packaged-theme target markers;
- `data-tn-theme-node`, a deterministic resource-local presentation address.

Existing bilingual pairing and source sanitization remain independent of CSS. Temporary translation markers such as `data-tn-id`, `data-tn-inline-id` and `data-tn-line` remain forbidden in published output; there is no blanket exemption for `data-tn-*`.

## 6. Protection and CSS safety

Semantic footnotes, endnotes and note references can receive presentation roles. This does not authorize changes to note content, IDs, hrefs, backlinks, ordering or structure. Navigation, package-declared cover/title pages, SVG, MathML, scripts, styles, forms, code/preformatted content, ruby annotation text and preserved source ranges retain their existing protections.

Only `builtin:chinese-reading` enables the fixed marker presentation policy, and only for a Chinese target. Forward references and backlinks keep their original anchor elements, IDs, names and hrefs, but their owned visible marker text becomes the real character `注`; the anchor tail and note prose are untouched. The host adds compatible EPUB/ARIA reference, backlink and note-body semantics without overwriting conflicting explicit roles or types. The packaged CSS renders both marker directions as a solid `#946126` circle with light `#fff8ef` text and normalizes nested superscript sizing. It uses no pseudo-content, hidden original text, scripts, images or bundled fonts. Reader support for popup notes and exact CSS rendering varies; ordinary href/backlink navigation remains the required fallback.

If the source anchor has no existing EPUB namespace binding, the renderer adds only the collision-free declaration required for the Clark-notation `epub:type` attribute. Verification accounts for that exact namespace change and reverses it while preserving original prefixes and descendant shadow declarations; it does not perform archive-wide namespace cleanup or rebinding.

Protection forbids direct marker attachment, resets or inline-priority demotion on protected nodes. Natural inheritance from a themed ancestor can still affect appearance. Fixed-layout resources remain unchanged with a warning. Mixed target/preserved scopes without an existing safe boundary fail instead of gaining arbitrary new wrappers.

Theme CSS is parsed with `tinycss2`; selectors use the existing Soup Sieve matcher on a read-only projection. The authoritative write path remains the existing DOM renderer. Supported CSS is a restricted reading-style language, not a browser engine:

- Static selectors and structural pseudo-classes are supported; dynamic interaction selectors, pseudo-elements and unsupported namespace syntax are rejected.
- Qualified style rules and light/dark `prefers-color-scheme` media rules are supported. Other at-rules, resource URLs, `@import`, `@font-face`, generated content, custom properties/`var()`, animations/transitions and destructive layout properties are rejected.
- Accepted property families cover typography, text/color, spacing, borders, shadows, hyphenation/word breaking and pagination. Unknown properties fail rather than being silently ignored.
- Token safety does not prove every accepted property value renders as intended in a reader. No external theme resources are fetched.

The compiler resolves matching rules to exact node addresses and emits important declarations. It preserves original external CSS and resources. Conflicting original inline-important declarations are demoted only for admitted property families, with unconditional or complete light/dark override coverage; otherwise the themed operation fails. Original values and unrelated declarations remain intact. Shorthand normalization can change priority for the whole admitted property family.

Generated selectors use bounded specificity guards above the original author stylesheet. The maximum guard count is 128; unsupported source cascade layers, nesting, unsafe/unresolved imports and malformed cascade boundaries fail explicitly. Source `base`/`xml:base` rebasing is unsupported. Unthemed compatibility is not narrowed by these theme-only checks.

Reserved markers, `tn-theme-never`, generated manifest IDs and resource paths cannot collide with the source. Reassembly starts from immutable source and saved translations, so generated styles and markers do not accumulate. General CSS precedes bilingual CSS. Reader user styles can still override book styling; neither precedence over user styles nor identical appearance across readers is promised.

## 7. Ownership and public contracts

| Owner | Responsibility and boundary |
| --- | --- |
| `epub/layout.py` | Immutable `LayoutNode`, `LayoutInventory`, `LayoutAssignment`, `LayoutProfile`, source fingerprints and strict persisted data validation; no renderer or model imports |
| `assemble/epub/layout.py` | Original source inventory using existing projection, safe archive/CSS readers and merged-paragraph logic |
| `agents/layout_analyzer.py` | Model prompt and strict ID-addressed observation protocol; injected role vocabulary, no assembly/pipeline imports |
| `pipeline/nodes/layout_analysis.py` | Deterministic sampling, held-out checks, conflict expansion and batch checkpoints |
| `pipeline` | Configuration, goal sequencing, state/identity/cache admission, analyst construction, usage and output fingerprints |
| `assemble/epub/rendering/theme` | CSS-only immutable bundle, profile binding, exact-address CSS plans and archive application; imports layout data, not inventory construction |
| `assemble/epub/rendering/source_archive.py`, `generated.py` | Explicit source-to-target bindings through real rendering transformations |
| `assemble/epub/verification` | Independent plan admission, exact reversal and existing preservation checks |
| `assemble/epub/publication.py` | Verify all requested outputs before sequential durable publication and receipts |
| `pyproject.toml`, release workflow and smoke scripts | Packaged CSS and real offline frozen application assembly on the existing five targets; no embedded classification runtime |

Callable boundaries:

```python
build_layout_inventory(source_path, chapters)
analyze_layout(inventory, analyzer, *, checkpoint, save_checkpoint)
resolve_theme(styles, bilingual_styles, *, config_base_dir=None, origins=None)
semantic_output_digest(
    bundle, *, out_format, mono, bilingual, bilingual_order, layout_digest=None
)
ThemeService(bundle, *, layout=None)
ThemeService.preflight_source(path)
ThemeService.plan_archive(path, scopes, *, bilingual)
ThemeService.apply_archive(path, plan)
ThemeService.render(path, scopes, *, bilingual)
```

`ThemeBundle` contains `general_css`, `bilingual_css`, `digest`, `policy_version` and `provenance`. General styling requires a validated profile; bilingual-only styling does not. The application constructs and injects the service. `ThemePlan` remains a transient, host-admitted expectation, not persisted executable state.

## 8. Independent verification and publication

Plan admission independently reconstructs expected markers and CSS using the exact injected profile and bindings. It does not call the model again or trust an output-embedded self-report. The verifier reopens the EPUB, checks exact generated CSS, links, manifest entries, markers and inline normalization, then reverses only admitted changes on a verifier-only copy before existing content, bilingual, navigation and resource checks.

All requested EPUBs render and verify before any final path is replaced. Publication then performs sequential durable replacements and records physical receipts. A later I/O failure does not roll back an earlier successful file; multi-file atomicity is not promised. File synchronization remains mandatory. Windows' unsupported directory synchronization is reported rather than presented as POSIX-equivalent durability.

Model protocol, source identity, binding, CSS, archive or verification failures stop the requested operation without a body fallback or partial accepted profile. Errors must not expose raw book text or credentials. Prior completed translations remain available. Actual source/mono/bilingual triplet evidence stays bound to current file hashes, publication receipts and the output digest for benchmark admission.

## 9. Acceptance requirements and reader limits

These are integration requirements, not results inherited from the previous classifier implementation:

| Area | Required observable evidence |
| --- | --- |
| Generality | Different books and mixed-use classes distinguish body, quote, attribution, summary and notes without book-specific dispatch |
| Sampling | Held-out conflicts expand to per-node analysis; unknowns remain unknown; full long evidence and invalid protocols are handled explicitly |
| Cache and cost | Completed-run missing profile analyzes automatically with zero translation calls; cached reassembly makes zero layout calls; interrupted batches resume; refresh failure retains the previous artifact |
| Identity | Source/CSS inventory changes reject stale profiles; changed output CSS reuses valid profiles; malformed schema, forged fingerprints and unmappable assignments fail |
| Rendering | Source-backed/generated mono and both bilingual orders preserve text, source copies, notes, links, tables, emphasis and protected scopes |
| Verification | Tampered markers, CSS, inline values or generated resources fail independent admission and reversal |
| Output | TXT bypasses presentation; CSS/profile changes leave paid translated chapter state unchanged |
| Packaging | Actual offline frozen source/generated assembly on Linux x64/ARM64, macOS Intel/Apple Silicon and Windows x64, not merely imports or `--help` |
| Appearance | Actual EPUB reader views of quotes, attributions, summaries, notes and unknown roles in light/dark and narrow/wide layouts |

The offline assembly smoke uses a deterministic model boundary to exercise automatic analysis and cached reuse; it must not request a provider. Browser screenshots prove browser appearance only. Earlier reader checks and test totals do not establish acceptance of this cutover. Reader-specific contrast, inheritance and pagination limitations must be reported separately from archive correctness. WeRead upload was declined; do not upload private book files or claim it has been verified.
