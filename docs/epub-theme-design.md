# EPUB Theme and JavaScript Classification Design

Status: implemented; 827 offline tests and five-target frozen application assembly passed. Calibre Content-server checks are complete with documented reader-specific limitations. Desktop Calibre and WeRead are not claimed as verified; WeRead upload was explicitly declined.
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

| Existing source | Current contract |
| --- | --- |
| `trans_novel/config.py`, `_FileConfig`, `OutputConfig`, `Config.load/from_dict` | Strict output configuration, field-presence tracking and config-relative path provenance |
| `trans_novel/cli/app.py` and `cli/tools.py` | Existing output flags override configuration; the global `--config` selects configuration for translate, resume and tools assemble |
| `trans_novel/pipeline/composition/output.py` | Resolves source-bound saved selections against explicit fields and defaults |
| `trans_novel/pipeline/application.py`, `_setup_output` | Resolves one immutable bundle, constructs `ThemeService`, preflights EPUB outputs before paid work and saves effective selection under the existing lock |
| `trans_novel/pipeline/nodes/finish.py`, `AssembleNode.execute` | Reuses the injected service and output digest for both requested variants |
| `trans_novel/pipeline/planning/fingerprints.py` and `prescan.py` | Consume the same output digest in assembly fingerprints |
| `trans_novel/assemble/writer.py`, `preflight_epub`, `assemble_outputs` | Dispatches source-backed/generated EPUB or TXT; stages all requested EPUB variants together |
| `trans_novel/assemble/epub/rendering/source_archive.py` and `generated.py` | Complete ordinary rendering, apply the injected theme and return a transient `ThemePlan` |
| `trans_novel/assemble/epub/rendering/bilingual.py` | Owns source insertion and pairing; packaged CSS owns presentation |
| `trans_novel/assemble/epub/verification/theme.py` and `validation.py` | Independently check and reverse exact admitted theme changes, then reuse preservation checks without executing JS |
| `trans_novel/assemble/epub/publication.py`, `publish_epubs` | Verifies all requested EPUBs before sequential durable replacements; persists physical publication receipts and actual triplet evidence |
| `trans_novel/benchmark/epub_check.py` | Binds persisted triplet evidence to current source/output hashes, both publication receipts and the output digest |
| `pyproject.toml`, `.github/workflows/build.yml` | Package JS/CSS and the embedded runtime; standalone probes and actual frozen source-backed/generated mono/bilingual assembly passed on all five targets |

The current temporary attributes `data-tn-id`, `data-tn-inline-id`, and `data-tn-line` remain forbidden in published output. New permanent presentation markers must be explicitly distinguished; do not exempt every `data-tn-*` attribute.

## 3. Public configuration

The supported output section is:

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

`output.mono`, `output.bilingual.enabled`, and `output.bilingual.order` are the current output fields. Defaults remain mono and bilingual enabled, with `target_first`. Existing CLI `--mono/--no-mono` and `--bilingual/--no-bilingual` flags are explicit overrides.

The strict root schema accepts `llm`, `quality`, and `output`; legacy flattened output fields and the old boolean shape for `output.bilingual` are rejected. The latter error directs users to `output.bilingual.enabled`.

When both variants are disabled, output resolution normalizes to mono-only and records `output_selection_normalized` before planning and execution.

No new theme-specific CLI grammar is necessary: `--config` selects the full configuration for `translate`, `resume`, and `tools assemble`. Do not add independent CLI fields for script and CSS paths that could form a partially overridden theme.

Configuration precedence per field:

1. Explicit CLI override where an existing flag exists.
2. Explicit field in the current configuration, including `override_theme: null`.
3. Persisted output selection for the existing run.
4. Built-in default for a new run.

Retain field-presence information before applying defaults. An omitted theme is not the same as an explicit `null`. A changed config file must not silently revert unrelated saved output choices.

Custom paths resolve relative to the selected config file, after expanding `~`. `Config.from_dict(raw, *, base_dir=None)` accepts an explicit base directory; relative custom theme paths without it fail rather than depend implicitly on the current directory. Resolved paths and their origins are persisted; paths are operational provenance, not semantic fingerprint inputs.

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

`add_bilingual_sources` retains source sanitization, direct-run boundary logic, omission rules for headings, language handling, and exact source/target pairing. Presentation comes from packaged or explicitly selected CSS, not a Python CSS constant.

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

The embedded runtime is `quickjs-ng==0.16.2.1`; the distribution name differs from its `quickjs` import. The host uses `Context` directly on its owning assembly thread, not the `Function` helper and its implicit executor. No asynchronous application call graph or external Node.js process is introduced.

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

Explicit native allocation/stack diagnostics retain `theme_limit`; an exception represented only
as `null` cannot be distinguished reliably from a script's `throw null`, so it is reported as a
sanitized script failure rather than falsely claiming precise out-of-memory attribution.

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

The builder only writes a wheel; it does not install it. Build it with:

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
the application's other dependencies. The release runtime gate passed on all five targets in
[workflow run 34712113015](https://github.com/turygo/wenyi/actions/runs/34712113015) at commit
`35b7df6`: Python 3.10 and 3.12 source probes exercised classification, limits, and CSS loading,
and each Python 3.12 frozen probe ran with an empty `PATH`. The macOS Intel job built the pinned
ABI3 wheel before installing the project, then reused it for both interpreter probes. This closes
the runtime prerequisite; it does not prove production DOM integration or reader appearance.

## 8. Ownership and public contracts

Theme implementation lives under `trans_novel/assemble/epub/rendering/theme/`, split into contracts/loading, snapshots/classification, CSS compilation and archive service responsibilities.

Current callable boundaries (signatures shown for reference):

```python
resolve_theme(rules, styles, bilingual_styles, *, config_base_dir=None, origins=None)
semantic_output_digest(bundle, *, out_format, mono, bilingual, bilingual_order)
ThemeService(bundle)
ThemeService.plan_archive(self, path, scopes, *, bilingual)
ThemeService.apply_archive(self, path, plan)
ThemeService.render(self, path, scopes, *, bilingual)
```

`resolve_theme` returns `ThemeBundle`; `semantic_output_digest` returns a string.
`plan_archive` and `render` return `ThemePlan | None`; `apply_archive` returns `None`.
`scopes` maps resource paths to `ResourceThemeScope`, carrying excluded element paths,
source/target pairings and whole-resource preservation. `render` plans and applies to the
temporary archive; source-backed and generated writer callbacks return that plan to publication.

- `ThemeBundle`: immutable script bytes, general CSS bytes or absent, bilingual CSS bytes, semantic digest, API/engine/policy versions, and path provenance. Read selected files once per invocation before preflight; never reread between mono and bilingual output.
- `RoleAssignment`: resource-local node ID, role, optional heading level. No text or arbitrary attributes from JS.
- `ThemePlan`: typed per-resource expected marker additions, exact inline before/after declarations, CSS bytes/hash, manifest/link additions and pre-theme resource hash. It contains only operations admitted by the host policy.
- `ThemeService`: an injected concrete service owning the resolved bundle and resource execution. No plugin registry, abstract factory, or provider interface is needed.

The frozen contracts reuse existing resource paths and element paths, not a second live DOM/archive model. Plans are transient verifier expectations, not persisted executable state.

`Application` constructs the service and injects it into `AssembleNode`/writer. Configuration carries selections, not live JS contexts. Planning receives the semantic digest as plain data; state stores selections, digests and receipts, not engine objects. Publication imports `ThemePlan` and `ThemeError` through verification's public facade, preserving `publication -> verification -> rendering`; rendering does not import verification.

## 9. Publication and independent verification

The writer callback finishes ordinary rendering and theme application before `prepare_publication` calls `verify_epub(..., theme_plan=...)`. There is no after-publication ZIP patching.

`publish_epubs` renders and verifies every requested EPUB before replacing any final path. It then replaces files sequentially and persists each physical result. A later I/O failure does not roll back an earlier successful replacement: exact durable `published_outputs` receipts remain available in `store.load_epub_verification()`. There is no multi-file atomicity promise; failure to persist a receipt is itself a publication error.

Windows CRT does not support directory file descriptors. Usage persistence skips that unsupported directory sync while retaining file `fsync` and atomic replacement; EPUB publication records `directory_fsync_unsupported` in its receipts. This does not promise POSIX-equivalent directory durability across power loss. POSIX directory permission errors and all file-sync errors remain failures.

Validate a theme plan against the pre-theme tree before any mutation: all addressed nodes exist, belong to the correct resource, are unprotected, and only admitted attributes/declaration priorities/resources are changed. The verifier receives these host-validated expectations independently of the output archive; do not read an output-embedded self-report as authority.

After reopening the EPUB, verify exact generated stylesheet bytes, manifest entries, links, markers, and authorized inline normalization. Reject missing, duplicated or unexpected theme artifacts. On a verifier-only tree copy, reverse only these exact changes, then run existing text-slot, bilingual, DOM, navigation and resource preservation checks. Unexpected original attributes/text remain failures.

Do not rerun JS during verification: verification checks the admitted plan and preserved content, not a second potentially stateful script result. This does not claim to prove the aesthetic correctness of a trusted classifier; visual tests cover that separate concern.

Fixed `BILINGUAL_CSS` comparisons have been removed; packaged default CSS is the sole source of default appearance. Pairing/source-copy checks remain independent of custom presentation.

For source-backed mono+bilingual publication, the report also records the actual source/mono/bilingual triplet, including file hashes and structural diagnostics. Per-file verification and valid theme projection are production publication gates; a generic structural failure inherited from the source is not a new gate. Benchmark admission uses `benchmark.epub_check.validate_epub_triplet(..., publication_report=..., output_digest=...)` to require matching current file hashes, digest and both durable receipts. It returns the recorded triplet rather than inferring alignment from two individual `passed` flags. A known-valid CI fixture must pass triplet structure as fixture acceptance, not as a new policy for arbitrary user books.

## 10. Persistence, preflight and fingerprints

Persist each validated effective output selection in source-hash-bound `output_selection.json` under the runner lock, after theme preflight and before paid execution. A run with an existing manifest first passes the existing identity check; a run without one binds the selection to `source_bytes_hash(input_path)` and may save it before the first manifest. This ordering is required because `PrepareNode` performs paid language detection before `RunIdentity`, while `AnalyzeNode` writes the first durable manifest. Keep successful output digest and artifact records separate from selection state, and update them only after durable publication. Save a compact role/protection/warning summary with a successfully published EPUB; do not persist full book text or script snapshots in logs.

For EPUB, the semantic output digest includes exact selected JS/CSS bytes, built-in asset bytes, script API version, theme compiler/policy version, engine package version, effective output choices, and bilingual order. Paths and file mtimes are excluded. Moving identical files does not invalidate output; editing bytes at the same path does.

For TXT, compute a format-aware digest that excludes all presentation asset bytes and the engine package version, and do not read the JS or CSS files. Retain the saved EPUB selections unchanged so a later EPUB assembly resolves the same choices.

Compute the applicable format-aware digest once and reuse it in both `assemble_input_fingerprint` call sites and the writer. The digest is absent from prepare/analyze/translate/polish/titles fingerprints. Theme edits invalidate only EPUB assembly/output verification, never translation targets or preparation identity.

Older state without output selection follows current config/default resolution. Older assembly records without a theme digest rebuild once; there is no alias for a legacy hardcoded theme implementation. A missing saved custom file fails clearly rather than silently choosing built-in styles.

Preflight validates script entry point, CSS syntax/policy, source cascade constraints, collisions and archive safety before paid work. Source-backed preflight uses synthetic target text, so classification against actual translated text can still fail at final assembly. Rendering or verification failure occurs before replacement and preserves completed translations and prior final files; later publication I/O failures follow the partial-success policy above. Correct the rule and run `trans-novel --config config.yaml tools assemble book.epub --format epub` to retry without LLM calls.

## 11. Error behavior and observability

Use stable error codes with config path, resource label and node ID where relevant, never raw book text or credentials.

| Code family | Examples | Outcome |
| --- | --- | --- |
| `theme_config` | invalid asset reference, unreadable saved asset, missing engine | Fail before paid work when detectable |
| `theme_script` | syntax, missing classify, exception, invalid return | No publication; retain translation state |
| `theme_limit` | CPU, memory, snapshot/result size | No partial output or silent fallback |
| `theme_css` | forbidden rule, unsupported source cascade, invalid selector | Fail themed operation; do not alter unthemed behavior |
| `theme_collision` | reserved attribute/ID/resource | No source overwrite |
| `theme_verify` | output differs from admitted plan | Existing verification failure transaction |

Unknown YAML fields, wrong types and relative paths without a base directory are rejected by configuration validation before theme loading; they are not all wrapped as `ThemeError`.

Source CSS using HTML `base` or XML `xml:base` declarations is rejected with
`source_base_unsupported`; the compiler does not guess reader-specific URL rebasing.

Zero matches for a valid role/style rule, fixed-layout exclusion, and unavailable safe direct-run styling scope are named warnings. Report whole-book zero role coverage prominently. Individual roles legitimately absent from a book do not fail it.

Use existing events/report facilities for resource counts, role counts, normalized declaration counts, protected scopes and stylesheet hashes. Do not introduce telemetry, a new report subsystem or a new CLI command merely for this feature.

## 12. Implementation sequence and ownership

Phases 1–6 are implemented and checked; reader-specific limitations are recorded in section 14:

1. **Runtime/package proof.** Python 3.10/3.12 source probes and separate Python 3.12 frozen compatibility probes passed on all five targets, including the pinned macOS Intel source build. These probes are not application assembly proof.
2. **Config and immutable assets.** Strict output schemas, presence-aware precedence, source-bound saved selection, packaged assets and path origins are implemented. Both example configurations remain aligned.
3. **Classification and CSS mechanism.** Bounded snapshots, target projection, protected scopes, script validation, exact-address CSS and inline-normalization ledgers are implemented.
4. **Source-backed and generated assembly.** Both renderers use the injected service for mono/bilingual EPUB output from EPUB, FB2, TXT and Markdown. TXT output bypasses presentation loading and execution.
5. **Verification and state cutover.** Independent expected-plan verification, both assembly fingerprint sites, durable partial receipts and actual triplet evidence are wired. Fixed Python bilingual CSS is removed.
6. **Acceptance and packaging.** All 827 offline tests and actual single-file application smoke passed on Linux x64/ARM64, macOS Intel/Apple Silicon, and Windows x64. The smoke assembles source-backed and generated EPUBs with an empty child `PATH`, verifies both physical receipts and role/CSS evidence, and confirms unchanged source/targets/usage. The Calibre Content-server comparison covers 24 fixed-width views; its inherited table-contrast and live-resize behavior are reported separately. A separate runtime probe is not substituted for application assembly.

Main owns final integration evidence and acceptance. Concurrent documentation/release workers skip build, lint, format and tests until integration; no worker may invent a new publication policy or a per-book compatibility branch.

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
| Persistence | Effective selections are written under the runner lock after theme preflight and before paid execution; existing manifests pass identity verification first, while new runs bind pre-manifest selections to the source hash. Output digest/artifact records remain unchanged until successful publication |
| Transactions | All requested EPUBs render and verify before replacement; later per-file I/O failure leaves prior final files intact and already replaced files `published=True`, without a multi-file atomicity promise |
| Packaging | Phase 1's source probe works with Python 3.10/3.12 on every target and its separate Python 3.12 frozen probe works on all five targets without requiring Node or bundled fonts; the probe is absent from release archives, and phase 6 exercises real theme assembly through each packaged `wenyi` executable |

Visual comparison uses the same translated text, DOM and source resources on both sides: left uses the English source book's CSS; right applies the general theme on top. Do not use the Chinese reference book's CSS as the before-state, and do not substitute unrelated demonstration prose. The local Black Swan pair is a manual reference, not a copyrighted repository fixture or a hardcoded rule source. Additional offline generated fixtures establish generality.

Check chapter openings, ordinary paragraphs, quotes, tables, inline emphasis and bilingual blocks in light/dark appearances at narrow and wide widths. Browser screenshots prove browser rendering only; verify the resulting EPUB in at least one independent EPUB reader. WeRead was part of the original acceptance scope, but the user explicitly declined book upload: do not send files to it, and report it as unverified. Report reader-specific behavior separately from archive/DOM correctness.

Canonical integration gates after implementation:

```bash
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_architecture.py
uv run --no-sync python -m unittest discover -s tests
uv build
```

Focused tests belong in the existing capability-mirrored `tests/assemble/`, `tests/pipeline/`, and CLI/config modules. Keep regression cases for plausible failures above; do not write tests that merely pin CSS text, private wiring or field forwarding. Release workflow smoke checks must actually classify and assemble an offline sample, not stop at `--help`.

## 14. Acceptance evidence and reader limitations

- Calibre 9.14.0's actual Content-server EPUB reader opened byte-matched local copies of the before, themed mono and bilingual publications. The 24 captured views cover chapter openings, ordinary text, inline quotation/emphasis and the original table at 430×900 and 1000×900, using native White/Black schemes with `Override all book colors: Never`. No generated book DOM or stylesheet was edited for screenshots.
- The inspected themed reference EPUB contains no `blockquote` elements, so its inline quotations and italics are not claimed as quote-role coverage. Unsupported CSS still fails through the independent admission/verification path; screenshots do not replace that gate.

Observed local evidence: the actual CLI assembled an unthemed mono before-state and themed mono/bilingual after-states for Black Swan in a deny-network sandbox using copied completed state. Source, translated-target and `usage.json` hashes were unchanged; physical publication receipts passed. The themed mono had 134 resources and 3,209 body roles; bilingual had 134 resources and 6,364 body roles. These are archive/CLI observations, not visual acceptance.

The before-state chapter heading was right-aligned at 20.6667px and ordinary text was 16px without an indent. The themed heading was centered at 25.6px in the configured red; ordinary text was 18.4px with a 36.8px first-line indent. Original italics remained italic. Bilingual source text followed the translation, at 14.08px with the intended light/dark foreground and background colors. The table retained all 18 DOM rows and paginated in the actual reader.

Two reader-specific limitations remain visible rather than being hidden by source-CSS changes. In Black mode, both before and after table cells use white text on the original `rgb(228, 228, 228)` ancestor background; this is an inherited low-contrast combination, not a passing dark-table appearance. White mode remains legible. Live viewport resizing also retained Calibre's old horizontal page offset, so final cases were reopened at their fixed viewport before capture. The desktop QtWebEngine connection did not provide completed visual acceptance; only the Content-server reader is covered. WeRead is intentionally unverified because upload was declined.

All five actual frozen application jobs passed in [Actions run 34748606847](https://github.com/turygo/wenyi/actions/runs/34748606847), against `0cb4f5a69166c8128a2dea36f5c60b5460bc8b26`. Each binary produced source-backed and generated mono/bilingual outputs with an empty `PATH`; the source-backed triplet passed and source/targets/usage remained unchanged. Windows acceptance exposed and fixed unsupported directory handles, a POSIX basename used for a native temporary path, and a read-only file-sync handle. File-sync failures remain fatal. The Release job was skipped; no merge, tag or Release was performed.

Local evidence remains outside the repository in the `wenyi-theme-acceptance-0kw1r6pn` temporary directory: `evidence.json`, `reader-evidence.json`, 24 original screenshots and four contact sheets under `reader-shots/`. These private book files and screenshots are not repository fixtures. Publication hashes and unchanged source/targets/usage were checked again after reader inspection.

The latest full offline run passed all 827 tests. Ruff, the architecture gate, and wheel/sdist builds passed. The initial frozen smoke exposed missing navigation in the older minimal fixture; the smoke now reuses the existing valid EPUB3 navigation fixture and passes without changing or weakening production verification.

## 15. External references

- [QuickJS-NG Python wrapper](https://github.com/genotrance/quickjs-ng): embedding API, ownership/threading and packaging.
- [QuickJS-NG package metadata](https://pypi.org/project/quickjs-ng/): Python support and published distribution targets; inspected version 0.16.2.1.
- [tinycss2 API reference](https://doc.courtbouillon.org/tinycss2/stable/api_reference.html): token/declaration parsing, serialization and explicit limits of semantic validation.
