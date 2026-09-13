# 07 · Separate DOCX style policy, Word emission and block assembly

[Index](README.md) · [简体中文](../zh/refactoring/07-docx.md)

Status: implemented; baseline: `7471256`.

Implemented: shared prefix/range policies are independent of readers and pipeline services. DOCX style, numbering and block emitters now own Word operations; the export coordinator is 66 lines. Six pre/post export comparisons (Chinese/English targets, monolingual and both bilingual orders) produced identical Word XML and resources, including colored headings, mixed spans, list numbering and tables.

## Evidence

[`docx_writer.py`](../../trans_novel/assemble/docx_writer.py) has 737 lines, with `_emit_chapter_blocks()` at line 556 spanning 131. It combines OOXML details, font/color policy, list restarts, mixed-style slicing, bilingual paragraphs, table cells and chapter traversal.

There are two concrete cross-layer dependencies: the writer imports the reader-private `_text_has_visible_list_prefix`, and imports `proportional_range_placements` from `pipeline.docx_styles`. The latter is a pure fallback calculation located in a model/persistence service module. Extracting those shared policies has more value than simply splitting the writer in half.

## Proposed structure

| Module | Responsibility |
| --- | --- |
| `document_styles/docx.py` | Pure style metadata types, supported source style fields, visible-list-prefix recognition and proportional fallback placement. No python-docx document mutation, agents, stores or pipeline imports. |
| `assemble/docx_styles.py` | Apply run/paragraph style, target font and heading-color policy, and calculate render slices. |
| `assemble/docx_numbering.py` | OOXML numbering lookup and restart operations. |
| `assemble/docx_blocks.py` | Emit headings, normal/bilingual paragraphs and tables; traverse ordered chapter blocks and merge continuations. |
| `assemble/docx_writer.py` | Open/create the Word document, select export settings, call block emission and save output. |
| Existing reader/alignment service | Extract source metadata and perform model-backed alignment respectively, using the neutral style policy. |

Keep the new shared module format-specific. Do not force HTML DOM restoration and Word run styling into a universal rendering abstraction. Move only demonstrably shared pure logic first; other style policies can remain in the writer layer.

## Preserved behavior

Style spans inherit bold, italic, underline, color and size from source items, never arbitrary model properties. Uniform styles bypass alignment; mixed-style placements are trusted only when their target digest matches. Missing/stale placements retain the proportional source-range fallback.

Chinese translated text keeps the current SimSun policy; original bilingual text and untranslated source fallback do not acquire the target font. Other target languages continue leaving the target font unspecified. Preserve explicit source heading colors, heading navigation, paragraph alignment/shading and current bilingual order.

Lists retain their source numbering identity and restart behavior, and visible prefixes must not be duplicated. Tables remain basic tables with the current supported structure; preserve row/column positions and consecutive table grouping. Do not add merged/nested table support or change DOCX ingestion scope as part of this extraction.

No rendering helper calls an LLM or mutates formal chapter state. Continue using the existing consistent export snapshot and disposable export view. Changing a helper's location must not add another alignment pass.

## Slices and acceptance

1. Move pure prefix detection and proportional placement to `document_styles/docx.py`; update reader, writer and `pipeline/docx_styles.py` together, without private import shims.
2. Extract Word style and numbering operations, then block emission. Preserve the reader's current API and model alignment's operation IDs and metadata.
3. Add import-boundary checks: `document_styles` must not depend on pipeline; format helpers must not import `pipeline.docx_styles`. The top-level export adapter's snapshot-store dependency remains separate from that restriction.

Use `tests/test_docx.py`, `tests/test_metadata_language.py`, `tests/test_bilingual.py`, `tests/test_review_autofix.py` and `tests/test_assemble.py`. Compare normalized `word/document.xml`, numbering, styles, relationships and text order, rather than ZIP bytes. Cover source fallback, stale placement hashes, mixed styles, long-paragraph continuations, repeated list IDs, table cells and both bilingual orders. Include default output paths, explicit `--out` and concurrent snapshots.

Future font choices or new DOCX structures can then change one policy/emitter with focused tests. This design does not introduce new fonts, options or output semantics.
