# P10 · Multilingual translation and prompt internationalization

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p10-multilingual-internationalization.md)

2026-09-06: the experimental implementation now includes English CLI defaults, English instructions and comments, and explicit target-language metadata contracts. Real-model quality evaluation and selectable interface locales remain follow-up work.

## Using the first version

Retain your model configuration and choose a source and target. For Chinese-to-English:

```yaml
language:
  source: zh
  target: en
```

```bash
uv run trans-novel languages
uv run trans-novel --config config.yaml translate book.epub --bilingual
```

The first command lists built-in languages without an API key. The second translates directly through your configured provider, producing `output/book.en.epub` and `output/book.en-bi.epub`. Japanese-to-English uses `source: ja`, `target: en`; English-to-Japanese uses `source: en`, `target: ja`. Each invocation selects one direction and takes a file in the corresponding source language, without a Chinese pivot. Subtitles retain their separate lightweight path behind the same `translate` command.

Built-in codes are `zh`, `zh-Hant`, `en`, `ja`, `ko`, `fr`, `de`, `es`, `it`, `pt`, `ru`, plus regional variants `en-US`, `en-GB`, `pt-BR`, and `pt-PT`. Built-in means profiles, configuration, and workflow are available; it does not certify real-model quality for every direction.

`source: auto` retains model detection; targets cannot be `auto`. Unknown codes fail explicitly instead of being truncated. Compatibility aliases include `zh-Hans` / `zh-CN` → `zh`, `zh-TW` → `zh-Hant`, and `ja-JP` / `ko-KR` → `ja` / `ko`. These are product aliases: the registry defines supported tags, and this version is not an arbitrary BCP 47 parser. Language tags can encode scripts and regions; a tag standard does not establish translation capability. [RFC 5646](https://www.rfc-editor.org/info/rfc5646/)

## Implementation and previous assumptions

Although source/target fields already existed, body translation, polishing, titles, glossary extraction, digests, and synopses still instructed Chinese output. They now render for the selected target, including pair-dependent honorific guidance. English, Japanese, and Traditional Chinese no longer receive Simplified Chinese full-width punctuation rules.

All generated descriptive metadata, including every glossary `note`, style guidance, character descriptions, and references to characters in prose, is explicitly requested in the target language. Character and term `target` values contain translated or transliterated names. `source` and `aliases` retain original spellings for matching; original-language quotations remain valid evidence. JSON keys, Review actions, and segment identities are unchanged. Type and gender values now use English identifiers; legacy Chinese values normalize on read without rewriting saved rows. A list of style-guide bullets is accepted and retained as text when a model deviates from the requested string schema.

CLI help, progress, tables, and errors use English, as do code comments, docstrings, configuration comments, and prompt instructions. English instructions do not require English translations: generated prose still follows `language.target`, and the generated default configuration still selects `zh`.

Character guidance no longer recommends establishing gender from names or first-person forms alone; it requires source/context evidence. English `brother/sister` does not automatically establish relative age. This is a prompt constraint, not the P09 evidence store, and it cannot guarantee that a model never guesses. More conservative wording can leave more unresolved references for subsequent evidence gathering and review.

## Central resource directory

This directory exists and its resources ship with the package:

```text
trans_novel/i18n/
  __init__.py
  resources.py                 # package loading and prompt content fingerprint
  languages.py                 # registry, aliases, composition, resume validation
  prompts.py                   # strict single-pass string.Template rendering
  metadata.py                  # canonical enums and legacy value compatibility
  data/
    tasks/*.txt                # translation, titles, analysis, Review, subtitles
    languages/registry.json    # supported codes and explicit aliases
    languages/zh.json          # source, target, terminology, and punctuation rules
    languages/en.json
    languages/ja.json
    languages/zh-Hant.json     # explicit inheritance and overrides
    languages/…
    pairs/registry.json
    pairs/ja__zh.json          # Japanese-to-Chinese honorific examples
    shared/honorific.json
    shared/guidance.json       # shared evidence constraints
    shared/metadata.json       # legacy enum mappings
    shared/metadata_guidance.txt # output language and original-name contracts
    shared/review_evidence_tools.txt
    export/about.zh.xhtml
    export/about.en.xhtml
```

`agents/prompts.py` and `agents/langprofile.py` remain compatibility entry points. Agents, SRT, and CLI use the same pure top-level language service. `i18n` imports no Pipeline, RunStore, Agent, or provider. Orchestrator assembly and responsibilities remain unchanged.

Rendering combines task protocol, source understanding, target expression, and a small number of pair-specific differences. A new language generally needs a profile and registry entry rather than full prompt copies for every pair. Regional variants explicitly inherit one base profile; Traditional Chinese does not silently fall back to Simplified Chinese.

Profiles contain `label`, `english_name`, `source_guidance`, `target_guidance`, `term_guidance`, `punctuation_rule`, `title_rule`, `digest_length`, and `synopsis_length`. Add tests and bilingual support-list updates with new resources. Profiles are data and do not import arbitrary Python functions from configuration; deterministic Simplified Chinese punctuation remains in the existing pure postprocessor.

Missing template variables fail immediately. Dollar signs and JSON braces in source text are substitution values and are never recursively interpreted. Existing JSON protocols and annotation-reference constraints remain covered by code and tests. Resources load through `importlib.resources` independently of the working directory; Python supports package resources that are not ordinary filesystem directories. [Python 3.10 importlib.resources](https://docs.python.org/3.10/library/importlib.html#module-importlib.resources)

## State isolation and compatibility

New default `zh` projects retain `state/<slug>/`; other new targets use `state/<slug>/targets/<target-language>/`. Subtitles use `state/srt/<slug>/targets/<target-language>/`. Each target owns chapters, glossary, analysis, context, Review, accounting, and lock scope. Subtitles still have no glossary or Review.

If a legacy root already stores a non-Chinese target, matching runs resume there. Different targets use `targets/`, including `targets/zh/` in that case. Existing directories are never automatically moved, cleaned, or overwritten. Full `source_sha256` validation still prevents reusing a target project for different content with the same filename.

State-oriented commands locate the configured target. An explicit source conflicting with saved state fails, while `auto` can restore the saved detected source. The manifest no longer silently switches a newly requested target back to the old one. Reverse translation is a separate run, without mixing multiple formal targets in one project.

Derived initialization state still precedes the final manifest commit. Completed batches skip calls; resource updates do not automatically retranslate completed segments, saved analysis, or glossary notes. New manifests record a resource fingerprint, activation events record the current resources, and Review cache identity includes source/target languages, honorific strategy, and the resource fingerprint. Resuming after a resource update can mix previously completed translations with newly generated work; use separate `paths.state_dir` values for whole-book comparisons.

## Export scope

Default names are `<stem>.<target-language>.<extension>` and `<stem>.<target-language>-bi.<extension>`. Default Simplified Chinese retains `.zh.*`. Explicit `--out` naming and its `-bi` derivative retain their existing behavior. Separate style editions of the same target may still share a default output name and require distinct explicit output paths.

EPUB/HTML metadata follows the target. DOCX retains its existing font policy: Song for Chinese targets, without forcing Chinese fonts on others. Both Runtime and the export view check the target before deterministic Simplified Chinese punctuation conversion, including direct export API calls. Export transformations remain confined to memory copies, preserving formal text and existing annotation/style identity boundaries.

The Chinese EPUB about page moved into resources. Non-Simplified targets temporarily use an English page honestly tagged as English; disable it with `output.about_page: false`. This version does not translate every interface/about page and adds no RTL targets such as Arabic or Hebrew. PDF fonts, layout, and external BabelDOC bridge language capabilities need per-format validation; the MIT package adds no AGPL dependency.

## Validation and quality limits

New offline coverage includes the original four failing counterexamples; all six Chinese/English/Japanese directions through analysis, digests, translation, polishing, terminology, and Review; TXT/Markdown/HTML/EPUB/DOCX mono/bilingual output; explicit paths; target state and subtitle isolation; Traditional Chinese subtitles; completed-work skipping and interrupted resume; Review reruns after resource changes; concise invalid-language/YAML errors; resource rendering and language listing.

All responses use FakeClient and authored temporary fixtures. The new prompts have not yet undergone real-model before/after evaluation on a public-domain novel of at least 50,000 words. CONTRIBUTING requires that work before formal quality acceptance. Language-specific expression, fonts, oversized continuation spacing, and existing character-ratio warnings need further evaluation. Passing protocol tests or displaying the right language name does not establish translation quality.

PyInstaller now collects language resources and runs `languages` during release smoke testing. Local validation reports **611 passed, 44 subtests passed**, including 49 multilingual and metadata-language tests, plus passing Ruff check/format and whitespace checks. Wheel/sdist were built from an isolated copy of product source: all 54 resources match byte for byte, and `languages` runs from the wheel in another working directory. Packaging uses temporary version `0.0.0` for verification, not a release artifact. Cross-platform binaries and real PDF bridge behavior remain unverified.

## Follow-up stages

| Stage | Scope | Acceptance |
|---|---|---|
| I1: current experiment | Pure language service, external tasks, direct translation, target isolation, naming, English defaults, metadata contracts | Offline regressions, architecture checks, package-resource checks |
| I2: core language quality | Six Chinese/English/Japanese directions on public-domain books; scripts/regions, long sentences, titles, references, continuation spacing | P02 blind evaluation and separate reports per direction; unverified directions remain experimental |
| I3: instruction and interface locales | Separate instruction prose, analysis prose, CLI, and about-page locales | UI changes do not invalidate semantic caches; instruction changes remain traceable; explicit UI fallback |
| I4: more languages and layout | Native-language review, RTL, fonts/glyphs, per-format capability matrix | Validate each language/format; CSS direction alone does not certify RTL |

I3 can organize task instructions under `data/instructions/<locale>/`, add locale-specific language rules under profiles, and manage presentation strings in `data/ui/` and `data/export/`. Unimplemented `ui_locale`, `prompt_locale`, and target-fanout settings are not exposed today. UI resources may negotiate fallback; missing target rules should fail rather than silently change the requested output language. Language matching standards provide negotiation mechanisms, while translation support remains an application policy. [RFC 4647](https://datatracker.ietf.org/doc/html/rfc4647)

P06–P09 integration remains future work: evaluate retrieval models for each language; scope translation memory by direction and style; separate observed source style from target expression rules; retain language-independent entity/relation evidence before deciding whether the target requires age, gender, or politeness distinctions. These independent proposals are not implemented by this version.

## Implementation entry points

- [Language rules and validation](../../../trans_novel/i18n/languages.py), [rendering](../../../trans_novel/i18n/prompts.py), [resources](../../../trans_novel/i18n/data/).
- [Configuration](../../../trans_novel/config.py), [state location](../../../trans_novel/pipeline/runstore.py), [preparation](../../../trans_novel/pipeline/preparation.py).
- [Multilingual tests](../../../tests/test_i18n.py), [metadata contracts](../../../tests/test_metadata_language.py), [usage](../../usage.md), [configuration](../../configuration.md).
