# EPUB Theme and JavaScript Classification Design

Status: implementation specification; not an implemented feature.
Date: 2026-09-13.

## 1. Decisions and scope

The product needs one reusable classification script and one reading theme across books. It does not need a catalogue of book-specific patches.

Confirmed by the user:

- Configure output themes, including bilingual presentation, outside Python.
- Use trusted local JavaScript to classify document content using general rules, regular expressions, and structural context.
- Embed a JavaScript engine; do not require users to install Node.js.
- Execute only scripts explicitly selected and trusted by the user. Hostile third-party script execution is not a supported security promise.
- Adding an embedded engine dependency and `tinycss2` is approved. No dependencies are added by this document.
- Do not bundle fonts. Declare font families and fallbacks only.

The host owns text, translation/source pairing, DOM integrity, archive I/O, validation, and publication. JavaScript returns classifications, not DOM mutations. CSS owns appearance. Neither configuration nor JavaScript can disable preservation checks.

Non-goals: book-name/ISBN dispatch, per-book override tables, EPUB-supplied scripts, network-loaded themes, arbitrary DOM transformation plugins, a browser layout engine, LLM-based theme classification, and a theme marketplace.

Generality means configurable heuristics across different book structures. It does not mean infallible identification of every ambiguous heading or identical rendering in every reader.

## 2. Existing integration points

| Existing source | Observed contract | Required change |
| --- | --- | --- |
| `trans_novel/config.py`, `_FileConfig`, `OutputConfig`, `Config.load/from_dict` | YAML currently exposes only `llm` and `quality`; output choices are runtime fields | Add strict public output configuration and path provenance |
| `trans_novel/cli/app.py`, `_translate_impl`, `translate`, `resume` | CLI overrides mono/bilingual output; resume has a narrower signature | Route all entry points through the same output resolution |
| `trans_novel/cli/tools.py`, `assemble` | Rebuilds output from persisted translation state | Resolve the same theme without model requests |
| `trans_novel/pipeline/application.py`, `Application`, output preflight | Production composition root; preflights before paid translation | Construct and inject a resolved theme service; include theme preflight |
| `trans_novel/pipeline/nodes/finish.py`, `AssembleNode.execute` | Builds mono/bilingual outputs and records assembly fingerprint | Reuse one immutable theme bundle for both outputs |
| `trans_novel/pipeline/planning/fingerprints.py`, `assemble_input_fingerprint` | Fingerprint contains targets and output choices | Include the resolved output/theme digest |
| `trans_novel/pipeline/planning/prescan.py`, `build_prescan_inputs` | Independently computes assembly fingerprint | Consume exactly the same resolved digest |
| `trans_novel/assemble/writer.py`, `preflight_epub`, `assemble` | Dispatches source-backed/generated EPUB or TXT; publishes through a temporary file | Thread the theme service and verification expectations through both EPUB paths |
| `trans_novel/assemble/epub/rendering/source_archive.py` and `source_markup.py` | Preserve archive members and replace verified text slots | Apply theme only after ordinary rendering, without changing persisted source paths |
| `trans_novel/assemble/epub/rendering/generated.py` | Produces EPUB from other input formats | Use the same theme application path |
| `trans_novel/assemble/epub/rendering/bilingual.py` | Owns source insertion, reserved classes, direct-run handling and hardcoded `BILINGUAL_CSS` | Keep structural operations; move presentation to packaged CSS |
| `trans_novel/assemble/epub/verification/{package,validation,source,bilingual,slots,structure}.py` | Checks fixed bilingual CSS, resources, DOM and slots | Verify exact authorized theme differences instead of a fixed CSS string |
| `trans_novel/assemble/epub/publication.py`, `prepare_publication` | Renders, verifies, then publishes | Preserve transaction semantics and fail closed on theme errors |
| `pyproject.toml`, `.github/workflows/build.yml` | Python >=3.10; five single-file executable targets | Package JS/CSS data and native JS runtime; execute real runtime smoke checks |

The current temporary attributes `data-tn-id`, `data-tn-inline-id`, and `data-tn-line` remain forbidden in published output. New permanent presentation markers must be explicitly distinguished; do not exempt every `data-tn-*` attribute.

## 3. Public configuration

The complete new output section is:

```yaml
output:
  mono: true
  bilingual:
    enabled: true
    order: target_first
  override_theme:
    rules: builtin:general
    styles: builtin:chinese-reading
  bilingual_styles: builtin:bilingual
```

A user-authored theme uses the same fields:

```yaml
output:
  override_theme:
    rules: ./themes/classify.js
    styles: ./themes/chinese-reading.css
  bilingual_styles: ./themes/bilingual.css
```

`override_theme` defaults to `null`: no general role classification, marking, or typography override. `bilingual_styles` defaults to the packaged bilingual stylesheet and is applied only when bilingual content is actually inserted. This lets users customize bilingual presentation while retaining original book typography.

The object form requires both `rules` and `styles`. `builtin:` identifiers are a closed registry of packaged resources, not URL schemes. Unknown fields, unknown identifiers, empty paths, and wrong types fail validation. There are no book-specific matching fields.

`output.mono`, `output.bilingual.enabled`, and `output.bilingual.order` replace the existing internal flattened output fields in a clean cutover. Preserve existing defaults: mono and bilingual enabled, `target_first`. Existing CLI `--mono/--no-mono` and `--bilingual/--no-bilingual` remain explicit overrides. Do not rename those user-facing flags.

The current `_DEPRECATED_ROOT_KEYS` explicitly rejects `output`. Remove only that root-key rejection as the new strict output schema is introduced. Do not re-enable legacy output fields or accept the old boolean shape for `output.bilingual`; invalid legacy shapes must explain the new mapping. Cover this early rejection path in config tests, not just construction of `OutputConfig` in isolation.

Preserve the existing effective-output behavior when both mono and bilingual are disabled: normalize to mono-only and report that normalization. Perform it once during output resolution, before planning and execution, so both fingerprint sites and `AssembleNode` receive the same effective flags.

No new theme-specific CLI grammar is necessary: `--config` selects the full configuration for `translate`, `resume`, and `tools assemble`. Do not add independent CLI fields for script and CSS paths that could form a partially overridden theme.

Configuration precedence per field:

1. Explicit CLI override where an existing flag exists.
2. Explicit field in the current configuration, including `override_theme: null`.
3. Persisted output selection for the existing run.
4. Built-in default for a new run.

Retain field-presence information before applying defaults. An omitted theme is not the same as an explicit `null`. A changed config file must not silently revert unrelated saved output choices.

Custom paths resolve relative to the selected config file, after expanding `~`. `Config.from_dict` gains an optional keyword-only `base_dir`; relative custom theme paths without a base directory fail rather than depend implicitly on the current directory. Persist resolved paths with their origin; paths are operational provenance, not semantic fingerprint inputs.

TXT output continues to support bilingual ordering but does not run the JS engine or load presentation files. Log that configured EPUB presentation is inapplicable; do not fail TXT because an unused CSS file is absent.

## 4. Script API: classification, not scripting the application

### 4.1 Entry point

Use one UTF-8 classic script defining `function classify(node, document)`. Helpers and regular expressions in the same file are allowed. The host evaluates it inside a strict wrapper and captures that function. There is no module loader, `import`, `require`, or `export` syntax in API version 1. Earlier discussion examples using `export` were illustrative, not a committed module API.

The host invokes the classifier in document order inside one JS batch per XHTML resource, not via one Python/JS round trip per node. Each resource gets a fresh context. `document` and node snapshots are deeply frozen JSON data, never live DOM or Python objects.

```js
function classify(node, document) {
  if (node.epubTypes.includes("subtitle")) {
    return { role: "subtitle" };
  }
  if (/^h[1-6]$/.test(node.tag)) {
    return { role: "heading", level: Number(node.tag.slice(1)) };
  }
  const shortStandalone = node.isTextBlock &&
    !node.textTruncated && node.textLength <= 80 &&
    !node.context.inTable && !node.context.inNavigation;
  if (shortStandalone && /^(?:chapter|part)\s+(?:[0-9]+|[IVXLCDM]+)(?:\s*[:.\u2014-]\s*\S.*)?$/iu.test(node.text)) {
    return { role: "heading", level: 1 };
  }
  if (node.tag === "blockquote") return { role: "quote" };
  if (node.tag === "p") return { role: "body" };
  return null;
}
```

This example demonstrates the API, not the entire bundled heuristic. The bundled classifier must also cover Chinese chapter numbering and avoid matching prose that merely discusses a chapter. Rules use semantic attributes and structural context before weak naming or text evidence. No title, filename, publisher, or ISBN exceptions may enter the bundled classifier.

### 4.2 Input schema

```ts
interface NodeSnapshot {
  id: number;
  parentId: number | null;
  previousSiblingId: number | null;
  nextSiblingId: number | null;
  tag: string;
  namespace: string;
  classes: string[];
  epubTypes: string[];
  ariaRole: string | null;
  ariaLevel: number | null;
  language: string | null;
  text: string;
  textLength: number;
  textTruncated: boolean;
  isTextBlock: boolean;
  context: {
    inTable: boolean;
    inNavigation: boolean;
    inQuote: boolean;
    inList: boolean;
  };
}
interface DocumentSnapshot {
  apiVersion: 1;
  nodes: readonly NodeSnapshot[];
}
interface Classification {
  role: string;
  level?: number;
}
```

IDs are dense indices in the immutable snapshot, resource-local, and never serialized as temporary slot IDs. Parent/sibling references use those same IDs. The snapshot exposes no host filesystem paths, book metadata used for dispatch, credentials, source HTML strings, event handlers, or executable attributes.

Snapshots contain eligible elements in the target body, including inline elements; protected subtrees and inserted source copies are absent. Parent/sibling IDs refer to the nearest corresponding nodes in this projection, or null. `isTextBlock` is true for h1–h6, p, li, blockquote, td, th, dt and dd; it is also true for a div containing text but no descendant block candidate. It is false for inline spans and container divs with nested blocks. The context flags come from original ancestor semantics before projection, not from JS role assignments.

`text` is rendered DOM text from the translation projection before theme application, normalized by collapsing XML whitespace and trimming. It is not browser-computed visible text: CSS visibility and layout are not evaluated. Preserve non-XML spaces such as non-breaking spaces. `textLength` counts Unicode code points before truncation; `text` contains at most 2,048 code points. Empty nodes are valid.

Build text previews and structural context in bounded tree passes; do not repeatedly serialize or concatenate complete ancestor subtrees. No browser or computed-style dependency is introduced. CSS-derived font size is not part of this API.

### 4.3 Output and fallback

The function returns `null` or a plain object with only `role` and optional `level`. Roles match `[a-z][a-z0-9-]{0,31}`. `level` is an integer 1–6 and is accepted only for role `heading`. Custom role names require no host enum update. `undefined`, promises, arrays, unknown keys, non-finite values, and invalid roles fail before mutation.

`null` means no new explicit role. It does not implicitly turn a node into body text. Native headings/paragraphs are handled by the bundled classifier, not a second hidden Python classification engine.

Role identity is attached to explicitly classified elements only. Children inherit normal CSS properties; they are not all stamped with the parent's role, which would compound relative sizes. Script decisions do not rename tags, reorganize headings, change TOC levels, or change translation segmentation.

### 4.4 Translation projection and protection

Classification occurs after text rendering but before presentation changes. Exclude inserted bilingual source copies from classifier input. Project the target DOM as if those copies and tool-only direct-run wrappers were absent. This makes mono and bilingual target classification independent of original/translation display order.

Do not classify the following protected subtrees: navigation, package-declared cover/title pages, SVG, MathML, scripts, styles, forms, code/preformatted content, ruby annotation text, and semantically identified footnotes/endnotes/noterefs. Ordinary table cells and list items may be classified, but their structural ancestors cannot be rewritten. Source ranges marked `preserve_source` remain outside theme mutation.

Protection forbids direct mutation or reset of protected nodes, including presentation-marker attachment and inline-priority demotion. Protected descendants may still receive naturally inherited CSS properties from an eligible themed ancestor; the theme system makes no visual-isolation promise for protected scopes.

Fixed-layout resources are left unchanged with a named warning. Unknown resources are not guessed to be fixed-layout. Protection is determined from package/DOM semantics, not filenames. Mixed target/preserved content without an existing safe boundary fails if the requested theme would cross that boundary; do not wrap arbitrary source text to make the operation possible.

### 4.5 Bundled general classifier and reading style

Keep these decisions in the packaged JS/CSS assets, not Python branches:

| Priority | Evidence | Role |
| --- | --- | --- |
| 1 | Semantic subtitle or caption attributes, or `figcaption` | `subtitle` or `caption` |
| 2 | Native heading containing only an anchored chapter/part number | `chapter-number` |
| 3 | Other native h1–h6 or ARIA heading with a valid level | `heading`, preserving declared level |
| 4 | Short standalone chapter/part-number text outside tables, lists and navigation | `chapter-number`; a separated title suffix instead yields `heading` level 1 |
| 5 | A paragraph inside a quote, table, or list | `quote-text`, `table-text`, or `list-text`, in that precedence |
| 6 | A blockquote or an ordinary paragraph | `quote` or `body` |
| 7 | Other content, including ambiguous class-only headings | `null` |

Chinese chapter/part numbers must be full-block matches using the ordinary Chinese/Arabic number characters and chapter/section/volume/part suffixes; English numbers accept decimal or Roman forms after Chapter/Part. A title suffix requires a whitespace or punctuation boundary. Never promote a text preview truncated at the API boundary. Neighbor information is available for user refinements, but bundled classification must not guess from publisher-specific class names. Preserve ARIA heading-level evidence in the snapshot as an additional nullable integer `ariaLevel`, range 1–6.

The packaged general CSS adopts the approved sample's typography, not a wholesale copy of the reference publisher stylesheet:

| Role | Default presentation |
| --- | --- |
| `body` | Local Source Han/Noto CJK/Songti serif fallback; 1.15em; line-height 1.5; justify; 2em indent; margin 1em 0 0 |
| `heading` level 1 | 1.6em; normal weight; center; red #de2432; line-height 1.5; margin 2em 0 1em; no indent |
| `heading` level 2 | 1.4em; weight 800; center; same red; margin 2em 0 1.3em |
| `heading` levels 3–6 | 1.3/1.2/1.1/1em respectively; left; same red; no indent; avoid breaks immediately after headings |
| `chapter-number` | 1em; center; same red; margin 2em 0 0.5em; no indent |
| `subtitle`, `caption` | 1em; no indent; retain surrounding structural layout |
| `quote` | Outer margins 1em 1em 0; no relative font scaling on the container |
| `quote-text` | Local Kaiti serif fallback; 1.15em; line-height 1.4; paragraph margin-top 0.5em |
| `table-text`, `list-text` | 1em; no indent; start alignment; margin 0.5em 0 |

Scope general typography rules to `content=target`; the separate bilingual stylesheet controls source copies. Ordinary nested spans may inherit family/size explicitly, but never apply those resets through emphasis-size, superscript, subscript, ruby or protected boundaries. Do not set general page foreground/background colors: let the reader control its light/dark surface. Default fonts are fallbacks, not a claim to reproduce WeRead's exact font selection.

## 5. Bilingual presentation

Keep `add_bilingual_sources`, source sanitization, direct-run boundary logic, omission rules for headings, language handling, and exact source/target pairing. Move only appearance out of Python.

Expose these permanent theme markers on eligible existing elements or already-generated safe wrappers:

- `data-tn-role="body"` and optional `data-tn-level="1"`.
- `data-tn-content="target"` for target role roots, `source` for generated original-text roots.
- `data-tn-theme-node="n123"` as a deterministic resource-local presentation address.

Keep existing structural classes required by source-copy validation; custom CSS must not need to know them. Source copies receive corresponding target role/level where there is an unambiguous pairing, not a second JS classification pass. If a source copy combines differently classified target ranges, apply only `content=source` to that common wrapper; do not invent a common role. Safe mapped descendants may retain individual roles.

Raw-text/direct-br cases must reuse current direct-run target wrappers. Do not introduce new wrapper kinds merely to host styles. If no safe element represents a requested scope, report an unapplied-scope warning rather than altering text topology.

General theme CSS is followed by bilingual CSS. Later matching declarations win within the tool's cascade. A custom bilingual file replaces the packaged bilingual file, rather than silently layering another hardcoded default under it. An intentionally empty bilingual stylesheet is valid.

The packaged bilingual CSS retains the current light/dark colors, relative source size, padding, border radius, and spacing unless the user changes its replacement. Source order continues to be DOM order controlled by output config, never flex/grid visual reordering.

Example bilingual stylesheet:

```css
[data-tn-content="source"] {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 0.88em;
  line-height: 1.55;
  color: #6b6b6b;
  background-color: #f4f3f0;
  padding: 0.5em 0.8em;
  margin: 0.2em 0 1em;
  border-radius: 5px;
}
@media (prefers-color-scheme: dark) {
  [data-tn-content="source"] {
    color: #a8a8a8;
    background-color: #2a2a2a;
  }
}
```

## 6. CSS contract and deterministic override

### 6.1 Accepted theme language

Use standard CSS declaration syntax, parsed with `tinycss2`; never split declaration strings on semicolons or remove `!important` with regex. Match selectors against a read-only marked DOM projection using BeautifulSoup's existing Soup Sieve integration. Do not introduce a second DOM parser for the authoritative write path.

Support selector lists, type/class/attribute selectors, combinators, and static structural pseudo-classes accepted by that selector engine. Reject dynamic interaction selectors, pseudo-elements, namespaces not explicitly supported by the matcher, and unmatched selector syntax. A valid selector matching nothing is a warning, not a syntax error.

Accept qualified style rules plus `@media (prefers-color-scheme: dark)` and `@media (prefers-color-scheme: light)`. Other at-rules fail explicitly. Reject `@import`, `@font-face`, all URL tokens/functions including escaped forms, generated `content`, custom properties/`var()`, animations/transitions, and visibility/layout-destructive properties. No external resources are loaded. This is a supported reading-style subset of CSS, not a complete browser CSS engine.

Allowed property families: font-family/size/style/weight/variant/stretch; line-height; letter/word-spacing; text-align/indent/decoration/transform; color/background-color; margin and padding; border and border-radius; box-shadow; hyphens; word-break/overflow-wrap; break-before/after/inside and their page-break counterparts. Shorthands in these families are accepted as whole property families. Include `-webkit-hyphens` and `-webkit-text-fill-color` where needed for reader compatibility. Reject unknown properties rather than silently ignoring them.

`tinycss2` tokenization is not semantic validation of every property value. Enforce syntax/token safety and the property allowlist; browser rendering remains an acceptance check for appearance. Never claim this parser proves arbitrary CSS values render correctly.

### 6.2 Application algorithm

1. Parse the ordinary assembled archive, record its resource hashes, and produce classifier snapshots before adding presentation markers.
2. Validate all classifications. Resolve custom CSS selectors on the marked projection. Filter protected nodes even if a selector would otherwise reach them.
3. Compile each matched rule to exact `data-tn-theme-node` addresses. Honor original theme rule order and media conditions; one stylesheet order, no hidden role priority list.
4. Emit all accepted theme declarations as important. Do not rewrite original external CSS or delete fonts/images.
5. For matching elements, demote the `important` bit on conflicting source inline declarations only, preserving their tokens/values and unrelated declarations. A shorthand intersects the entire corresponding property family; record that whole family as an authorized normalization. Demotion is allowed only when matching theme declarations cover that property family in every supported media state: either an unconditional base declaration or complete light and dark declarations. Otherwise reject the themed operation; do not add a compensating cascade engine. Do not delete or pretend to expand a `font` shorthand using tinycss2. Non-overridden shorthand components retain their value, but their priority can change; this is an explicit normalization consequence, not exact preservation of their previous cascade.
6. Give generated selectors strictly greater specificity than original author stylesheet selectors using repeated `:not(#tn-theme-never)` guards plus the exact node address. Verify that this reserved ID does not exist. A conservative bound is the total identifier-hash token count across all source stylesheet selector preludes, recursively including supported conditional rules, plus one; overestimation is acceptable. Attribute hashes may overestimate, never underestimate. Cap the guard count at 128 and fail explicitly beyond it.
7. Reject source CSS constructs whose cascade cannot be bounded by this algorithm, notably cascade layers and CSS nesting, when they affect a themed resource. Source stylesheet imports must be resolved through the archive's existing safe path rules; cycles, unavailable external imports, or unparseable selector structure fail theme preflight, not the unthemed path. Active animations/transitions on themed properties are unsupported and must be reported rather than claiming guaranteed override.
8. If source CSS is malformed such that the affected cascade or inline declaration boundary cannot be determined safely, fail the themed operation. Leave the existing unthemed compatibility path unchanged.
9. Emit one generated CSS resource per affected XHTML resource, register it in the OPF, and append one link in that resource's head. Per-resource sheets avoid one enormous all-book selector list and keep local node addresses unambiguous.

This algorithm handles source IDs and inline important declarations without a full original-style cascade evaluator. Fonts on nested spans require explicit inherited-property rules in the reusable theme, such as body spans receiving `font-family: inherit`; do not automatically reset every descendant's size or emphasis. Protect small/sup/sub/code/ruby semantics from broad normalization.

Readers can still apply user overrides or omit supported CSS features. Do not promise precedence over a reader's user stylesheet. The guard syntax and dark media behavior must be checked in actual target readers before shipping.

### 6.3 Collision and repeatability

Reserve `tn-theme-never`, `tn-theme-style-*` manifest IDs, theme resource paths, and the permanent markers above. Any preexisting source collision fails clearly; do not silently reuse or overwrite it. Applying twice to the same in-memory rendered archive is a programming error. Reassembly always starts from the immutable source and saved translation, so markers/links cannot accumulate.

Generated paths are relative to the OPF directory; links are relative to each XHTML. Preserve `mimetype` ordering/compression, untouched ZIP members, manifest identity, spine order, anchors, source stylesheets, and original resources.

## 7. Embedded runtime and trust model

Selected implementation candidate: `quickjs-ng`, distribution name distinct from its `quickjs` import. Current upstream metadata reports Python >=3.10 and resource limit APIs. Use `Context` directly on its owning assembly thread, not the `Function` helper and its implicit executor. No new asynchronous application call graph is introduced.

The resource context has no Python callbacks, module loader, filesystem, networking, process creation, timers, browser DOM, or credential access. Explicit user configuration is permission to execute that trusted file. Never discover executable themes inside an EPUB or persist-and-reload scripts from book content.

Before loading user code, remove `Date`, random-number APIs and pending-job/async host facilities, freeze the supplied data, and retain host references to serialization/validation helpers. The execution contract prohibits global-state-dependent results. Fresh contexts and fixed traversal order improve repeatability; they do not make arbitrary JS mathematically pure or constitute a hostile-code sandbox.

Initial internal limits, not YAML performance knobs:

| Limit | Value |
| --- | --- |
| Each JS/CSS input file | 256 KiB |
| Snapshot JSON per XHTML | 8 MiB |
| Snapshot nodes per XHTML | 50,000 |
| Text preview per node | 2,048 Unicode code points |
| Returned batch JSON | 4 MiB |
| Engine allocation | 64 MiB |
| JS stack | 512 KiB |
| Script initialization CPU budget | 1 second |
| Classification batch CPU budget | 2 seconds |

Exceeding a limit is an explicit error, never partial classification. Reconcile these limits with measured ordinary-book fixtures before release; changing a limit changes the engine policy version used by the output fingerprint.

QuickJS CPU interruption and allocation limits are not a hard wall-clock or whole-process RSS sandbox. A native engine defect can affect the host process. Do not advertise protection from malicious native exploits. The user chose trusted scripts, not untrusted third-party execution. Upgrading that trust model requires a separate design decision.

### Runtime release gate

Upstream currently lists wheels for Windows x64, Linux x64/ARM64, and macOS ARM64. macOS Intel is not listed among the current prebuilt wheel targets. Build from source on the existing Intel release runner and test the frozen executable. Do not silently drop this target or fall back to external Node.

Pin the tested engine version and retain its license. Test the runtime import, classification, CPU interruption, memory failure and CSS resource loading in all five produced binaries, without Node installed. Runtime import success in a development virtualenv is not proof of PyInstaller compatibility.

#### Pinned macOS Intel source assembly

The Intel build must not use the incomplete PyPI source archive: it omits
`quickjs-c-atomics.h` ([upstream issue 11](https://github.com/genotrance/quickjs-ng/issues/11)).
Build the ABI3 wheel from these complete, immutable upstream archives instead:

- Wrapper commit `f2ac026f5105b492b15b331bf1b71bd7a4e9cfb2`,
  [archive](https://codeload.github.com/genotrance/quickjs-ng/tar.gz/f2ac026f5105b492b15b331bf1b71bd7a4e9cfb2),
  SHA-256 `4a0679c607d8c27bc647ca0e61a203066e66865cc6970d93479e83c079eec760`.
- Engine 0.16.2 commit `1ab8676f4b6d6d669baeb5f21790fb9734636a20`,
  [archive](https://codeload.github.com/quickjs-ng/quickjs/tar.gz/1ab8676f4b6d6d669baeb5f21790fb9734636a20),
  SHA-256 `c788fe4f65c95ecfa4055c8778e7cb221f68fcc3315686627b0856da5c38514e`.

The wrapper tag alone is not the released assembly: its checked-in engine submodule points to
older commit `6d2a9bac`, and its `pyproject.toml` reports version `0.12.1.1`. The upstream release
workflow checks out engine 0.16.2 and stamps distribution version `0.16.2.1`. The repository
builder reproduces that procedure from the pinned commits, copies the engine license to
`LICENSE.quickjs`, and does not modify wrapper or engine runtime code.

The builder only writes a wheel; it does not install it. Build it for the upcoming production
integration with:

```bash
uv run --no-project --python 3.12 --with pip python scripts/build_theme_runtime.py
```

The approved Intel project-environment bootstrap strategy is:

```bash
uv venv --python 3.12
uv pip install --no-index --find-links build/theme-runtime-wheels quickjs-ng==0.16.2.1
uv sync --locked --group dev
```

The corresponding standalone installation strategy is:

```bash
uv tool install . --find-links build/theme-runtime-wheels
```

Only the engine wheel is local and index-disabled; normal package indexes remain available for
the application's other dependencies. On macOS ARM64, a disposable project explicitly requiring
the engine proved that locked sync retains the preinstalled native-extension bytes and that
isolated `uv tool install --find-links` selects the same locally built wheel. Both installations
executed the real classification, limit, and CSS probe successfully. This verifies the bootstrap
mechanism, not yet the Intel build or the future production application's dependency integration.

Evidence status: the workflow is configured to build this wheel once with Python 3.12 on Intel,
reuse it for the Python 3.10 source probe, and run the Python 3.12 frozen probe with an empty
`PATH`. No successful Intel build, frozen-probe result, or five-target CI pass is claimed yet.
This phase changes no production dependency, default, or theme availability.

## 8. Ownership and public contracts

Keep new theme implementation under `trans_novel/assemble/epub/rendering/theme/`. Start with contracts/loading, classification, and CSS/application modules; split by actual responsibility and governance limits, not one class per concept.

Suggested public boundaries, with all new names scoped to that capability:

```python
resolve_theme(settings, *, config_base_dir) -> ThemeBundle
classify_resource(snapshot, bundle) -> tuple[RoleAssignment, ...]
plan_theme(resource_snapshots, bundle) -> ThemePlan
apply_theme(archive, plan) -> None
```

- `ThemeBundle`: immutable script bytes, general CSS bytes or absent, bilingual CSS bytes, semantic digest, API/engine/policy versions, and path provenance. Read selected files once per invocation before preflight; never reread between mono and bilingual output.
- `RoleAssignment`: resource-local node ID, role, optional heading level. No text or arbitrary attributes from JS.
- `ThemePlan`: typed per-resource expected marker additions, exact inline before/after declarations, CSS bytes/hash, manifest/link additions and pre-theme resource hash. It contains only operations admitted by the host policy.
- `ThemeService`: an injected concrete service owning the resolved bundle and resource execution. No plugin registry, abstract factory, or provider interface is needed.

Exact parameter types should be the existing archive/resource structures where suitable; do not create duplicate DOM/archive models. New exported symbols require LSP reference checks before integration.

`Application` resolves and constructs the concrete service and passes it to `AssembleNode`/writer. Configuration models carry selections, not live JS contexts. Planning receives the resolved semantic digest as plain data; it must not import assembly/theme implementation. State stores structured selections and output digest, not engine objects. Verification may depend on rendering contracts under the existing capability direction; rendering must not import verification.

## 9. Publication and independent verification

The writer callback must finish ordinary rendering and theme application before `verify_epub` runs. Keep `prepare_publication`'s temporary-file and durable publication behavior. No after-publication ZIP patching is allowed.

Render and verify every requested EPUB output before replacing any final path. After all requested EPUBs pass, publish each file through the existing durable replacement path. If a later replacement has an I/O failure, files already replaced retain their existing `published=True` result; there is no rollback or multi-file atomicity promise.

Validate a theme plan against the pre-theme tree before any mutation: all addressed nodes exist, belong to the correct resource, are unprotected, and only admitted attributes/declaration priorities/resources are changed. The verifier receives these host-validated expectations independently of the output archive; do not read an output-embedded self-report as authority.

After reopening the EPUB, verify exact generated stylesheet bytes, manifest entries, links, markers, and authorized inline normalization. Reject missing, duplicated or unexpected theme artifacts. On a verifier-only tree copy, reverse only these exact changes, then run existing text-slot, bilingual, DOM, navigation and resource preservation checks. Unexpected original attributes/text remain failures.

Do not rerun JS during verification: verification checks the admitted plan and preserved content, not a second potentially stateful script result. This does not claim to prove the aesthetic correctness of a trusted classifier; visual tests cover that separate concern.

Replace fixed `BILINGUAL_CSS` hash comparisons and `style_shape_is_valid` assumptions throughout renderer exports and verifiers. Retain pairing/source-copy checks. Cleanly remove the old Python CSS constant after all callers and tests migrate. Packaged default CSS is the sole source of default appearance.

## 10. Persistence, preflight and fingerprints

Persist each validated effective output selection under the runner lock after run identity resolution and the first durable manifest write, before paid execution. Keep successful output digest and artifact records separate from selection state, and update them only after durable publication. Save a compact role/protection/warning summary with a successfully published EPUB; do not persist full book text or script snapshots in logs.

For EPUB, the semantic output digest includes exact selected JS/CSS bytes, built-in asset bytes, script API version, theme compiler/policy version, engine package version, effective output choices, and bilingual order. Paths and file mtimes are excluded. Moving identical files does not invalidate output; editing bytes at the same path does.

For TXT, compute a format-aware digest that excludes all presentation asset bytes and the engine package version, and do not read the JS or CSS files. Retain the saved EPUB selections unchanged so a later EPUB assembly resolves the same choices.

Compute the applicable format-aware digest once and reuse it in both `assemble_input_fingerprint` call sites and the writer. The digest is absent from prepare/analyze/translate/polish/titles fingerprints. Theme edits invalidate only EPUB assembly/output verification, never translation targets or preparation identity.

Older state without output selection follows current config/default resolution. Older assembly records without a theme digest rebuild once; there is no alias for a legacy hardcoded theme implementation. A missing saved custom file fails clearly rather than silently choosing built-in styles.

Preflight loads and validates configuration, script entry point, CSS syntax/policy, source cascade constraints, collisions and archive safety before paid work. Existing source-backed preflight uses synthetic target text, so classification against actual translated text can still fail at final assembly; state this explicitly. Final failures preserve completed translations and any previously published file. Correcting the rule and running `tools assemble` retries without LLM calls.

## 11. Error behavior and observability

Use stable error codes with config path, resource label and node ID where relevant, never raw book text or credentials.

| Code family | Examples | Outcome |
| --- | --- | --- |
| `theme_config` | unknown field, invalid path, missing saved asset | Fail before paid work when detectable |
| `theme_script` | syntax, missing classify, exception, invalid return | No publication; retain translation state |
| `theme_limit` | CPU, memory, snapshot/result size | No partial output or silent fallback |
| `theme_css` | forbidden rule, unsupported source cascade, invalid selector | Fail themed operation; do not alter unthemed behavior |
| `theme_collision` | reserved attribute/ID/resource | No source overwrite |
| `theme_verify` | output differs from admitted plan | Existing verification failure transaction |

Zero matches for a valid role/style rule, fixed-layout exclusion, and unavailable safe direct-run styling scope are named warnings. Report whole-book zero role coverage prominently. Individual roles legitimately absent from a book do not fail it.

Use existing events/report facilities for resource counts, role counts, normalized declaration counts, protected scopes and stylesheet hashes. Do not introduce telemetry, a new report subsystem or a new CLI command merely for this feature.

## 12. Implementation sequence and ownership

Each phase must satisfy its acceptance before the next begins. Do not change production defaults midway through migration.

1. **Runtime/package proof.** Run the source probe with Python 3.10 and 3.12 on every release target, then freeze and run it with Python 3.12 on all five targets, including a macOS Intel source build. Phase 1 uses a separate frozen `theme-runtime-probe` compatibility executable and keeps it out of release archives. Prove limits and no host callbacks. If a target fails, resolve the runtime dependency choice before integrating; do not ship a disabled theme on that target.
2. **Config and immutable assets.** Add strict output schemas, presence-aware precedence, saved selection, built-in assets and path handling. Produce one bundle/digest per invocation. Keep source and packaged config examples aligned.
3. **Classification and CSS mechanism.** Implement snapshots, projection, protected scopes, script validation, compiled CSS addressing and normalized-inline ledger. Reuse existing parsers and safe ZIP path handling.
4. **Source-backed and generated assembly.** Inject the same service into both paths; implement mono/bilingual scope mapping without new unsafe wrappers. Theme selection must work for EPUB output from EPUB, FB2, TXT and Markdown.
5. **Verification and state cutover.** Integrate expected-plan verification and both fingerprint sites; remove fixed bilingual CSS assumptions. Ensure failures leave source, state and prior published output safe.
6. **Acceptance and packaging.** Run offline behavioral tests, actual before/after rendering and all release smoke checks. Make every release target exercise real theme assembly through its built `wenyi` executable; the separate compatibility probe is not sufficient phase 6 packaging evidence. Update user configuration documentation, both example config files, architecture capability rules only if actually needed, and `[Unreleased]` changelog. Remove throwaway probes.

The main agent owns configuration/API/security decisions and shared integration. Future executors receive exact file ownership and shared contracts; concurrent writers skip build/lint/format/tests until integration. No executor may invent an error policy or add a per-book compatibility branch.

## 13. Acceptance matrix

Every case is an observable contract, not a source-text assertion.

| Area | Required proof |
| --- | --- |
| Generality | The same bundled JS/CSS handles independently generated books with native headings, paragraph-based headings and generic class variation; no per-book selection |
| Heuristic safety | A prose sentence mentioning a chapter is not promoted; long/truncated paragraphs are not regex-heading candidates |
| Config | Relative paths, explicit null vs omission, CLI precedence, resume selection and changed file bytes |
| Classification | Invalid/undefined/async results fail; regex loops and allocation exhaustion terminate; Unicode boundaries are deterministic |
| CSS override | Nested span fonts, source ID important rules, escaped CSS and font shorthand normalization; inline-important demotion succeeds with an unconditional family override or complete light/dark family overrides and fails for partial media-state coverage |
| CSS exclusions | Unsupported layers/nesting/import failures are explicit; no external fetch; protected nodes receive no direct markers, inline demotion or resets, while ordinary inheritance is permitted without a visual-isolation guarantee |
| Semantics | Emphasis, superscripts, ruby annotations, code, lists, tables, anchors, images and preserved source ranges survive |
| Bilingual | Both orders; mixed inline/direct-br/table cases; exact source copies; unchanged text pairing; custom light/dark source appearance |
| Output paths | Source-backed and generated EPUB; TXT never reads presentation assets, excludes their bytes and engine version from its digest, and retains saved EPUB selections |
| Verification | Tampered CSS, unexpected marker, altered inline value and duplicated generated resource fail; unthemed books retain existing validation |
| Resume | CSS/JS edits rebuild EPUB output with zero LLM calls and unchanged translated targets; identical bytes at a new path preserve the EPUB semantic digest |
| Persistence | Effective selections are written under the runner lock after identity and the first durable manifest but before paid execution; output digest/artifact records remain unchanged until successful publication |
| Transactions | All requested EPUBs render and verify before replacement; later per-file I/O failure leaves prior final files intact and already replaced files `published=True`, without a multi-file atomicity promise |
| Packaging | Phase 1's source probe works with Python 3.10/3.12 on every target and its separate Python 3.12 frozen probe works on all five targets without requiring Node or bundled fonts; the probe is absent from release archives, and phase 6 exercises real theme assembly through each packaged `wenyi` executable |

Visual comparison uses the same translated text, DOM and source resources on both sides: left uses the English source book's CSS; right applies the general theme on top. Do not use the Chinese reference book's CSS as the before-state, and do not substitute unrelated demonstration prose. The local Black Swan pair is a manual reference, not a copyrighted repository fixture or a hardcoded rule source. Additional offline generated fixtures establish generality.

Check chapter openings, ordinary paragraphs, quotes, tables, inline emphasis and bilingual blocks in light/dark appearances at narrow and wide widths. Browser screenshots prove browser rendering only; verify the resulting EPUB in WeRead and at least one independent EPUB reader. Report reader-specific behavior separately from archive/DOM correctness.

Canonical integration gates after implementation:

```bash
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_architecture.py
uv run --no-sync python -m unittest discover -s tests
uv build
```

Focused tests belong in the existing capability-mirrored `tests/assemble/`, `tests/pipeline/`, and CLI/config modules. Keep regression cases for plausible failures above; do not write tests that merely pin CSS text, private wiring or field forwarding. Release workflow smoke checks must actually classify and assemble an offline sample, not stop at `--help`.

## 14. Remaining evidence gates, not undecided product behavior

- The chosen embedded runtime must prove macOS Intel source build and PyInstaller compatibility. Current upstream wheel availability is not that proof.
- CSS specificity guards and conditional source styling require actual-reader acceptance. Unsupported source CSS must fail explicitly instead of undermining the override claim.
- Default classification quality and internal resource limits need the heterogeneous fixture matrix. Do not fit them solely to the Black Swan example.
- This document does not add production code, dependencies, runtime assets or migration state. Implementation is a separate task.

Local documentation probe completed on the current macOS ARM64 host using an isolated `uv run --no-project` environment and `quickjs-ng==0.16.2.1`: YAML examples parsed; CSS example tokenization succeeded; the displayed JS classified a chapter heading and ordinary prose differently; CPU interruption and allocation-limit failure triggered; tinycss2 preserved a font shorthand value while demoting its important flag. This is API/example evidence only, not proof of the full compiler, reader behavior, cross-platform packaging, or production integration.

## 15. External references

- [QuickJS-NG Python wrapper](https://github.com/genotrance/quickjs-ng): embedding API, ownership/threading and packaging.
- [QuickJS-NG package metadata](https://pypi.org/project/quickjs-ng/): Python support and published distribution targets; inspected version 0.16.2.1.
- [tinycss2 API reference](https://doc.courtbouillon.org/tinycss2/stable/api_reference.html): token/declaration parsing, serialization and explicit limits of semantic validation.
