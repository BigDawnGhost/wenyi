# Input and output formats

This reference describes supported book inputs, export choices, preservation limits
and source requirements. For workflow choices, see the
[translation guide](../guide/pipeline.md); for switches and settings, see the
[CLI guide](../guide/cli.md) and [configuration reference](configuration.md).

<a id="input-output-matrix"></a>

## Input/output matrix

| Input | CLI default output | Other export choices | Main preservation boundary |
| --- | --- | --- | --- |
| EPUB | EPUB | TXT, HTML, Markdown, DOCX | Backfill original XHTML/resources; not a page-layout guarantee |
| FB2 | EPUB | TXT, HTML, Markdown, DOCX | Rebuild chapters; no FB2 export or original FB2 template backfill |
| TXT | EPUB | TXT, HTML, Markdown, DOCX | Paragraphs and recognized headings, not rich layout |
| Markdown | EPUB | TXT, HTML, Markdown, DOCX | Heading hierarchy and paragraphs; not a full Markdown syntax round trip |
| HTML / XHTML | HTML | EPUB, TXT, Markdown, DOCX | Static source-file backfill; no browser execution |
| DOCX | DOCX | EPUB, TXT, HTML, Markdown | Reconstruct supported paragraphs, tables, lists and styles |
| PDF via MinerU | EPUB | TXT, HTML, Markdown, DOCX | Reflow converted HTML, not the original PDF pages |
| PDF via BabelDOC | PDF | EPUB, TXT, HTML, Markdown, DOCX | Fill translations into the saved external bridge session |

CLI `--format epub|txt|html|markdown|pdf|docx` overrides the default.
PDF is available **only for saved BabelDOC state** in CLI, Web and Desktop.
Changing the current `pdf_backend` setting does not convert an existing project or
change the backend recorded in its state. Web/Desktop provide their own export
selection UI; the table's automatic defaults describe CLI commands.

## Naming and bilingual output

CLI output defaults to an `output/` directory beside the source file:

| Edition | Default name | Example with English target |
| --- | --- | --- |
| Monolingual | `<source-stem>.<target-language>.<ext>` | `output/book.en.epub` |
| Bilingual | `<source-stem>.<target-language>-bi.<ext>` | `output/book.en-bi.epub` |

The target defaults to `zh`; Markdown uses the `.md` extension.
An explicit `--out editions/book.epub` keeps that path for the monolingual output
and derives `editions/book-bi.epub` for bilingual output, without injecting a
language suffix. The same `-bi` derivation applies when only bilingual output is
requested. Choose a suffix consistent with the selected format.

Only monolingual output is enabled by default. `--bilingual` enables the bilingual
edition; `--no-mono --bilingual` requests only that edition.
EPUB, HTML, TXT, Markdown and DOCX can pair source and translation.
PDF pairs each original page with its translated page side by side within one
wider page; see [BabelDOC](#babeldoc).

- `output.bilingual_order` selects target-first (default) or source-first.
  Paragraph-paired formats place that version first; PDF places it on the left.
- EPUB/HTML source text is subdued by default.
  `output.bilingual_preserve_source_style: true` inherits normal book styling.
- DOCX source text defaults to normal body styling, without forced gray text,
  highlighting or reduced font size. The same preservation option restores recorded
  source character styles, including mixed spans in paragraphs and tables.
- Untranslated EPUB paragraphs appear once as source, retaining links and ruby.
  Bilingual ordering does not create a duplicate source fallback.

Source styling and order are independent. These choices require only re-export,
not translation. Web downloads and Desktop save destinations are controlled by their
platform interfaces rather than the CLI's source-adjacent directory.

## EPUB

EPUB-to-EPUB export attempts to backfill the original XHTML templates, retaining
styles, images, table of contents, anchors and internal links. It merges split
continuations into their original logical paragraphs. Original source copies in
bilingual output retain their own markup without duplicating anchor IDs.

High-confidence footnote/endnote references supply their source note bodies as
translation context. Ordinary navigation links, backlinks and external links are
not borrowed as note content. Spine note resources remain normal translatable
content; the borrowed context is not appended to the body paragraph.

After translation, annotation alignment locates links against the fixed target.
Whole-source ranges can map directly without a model call; partial ranges and point
notes may need model requests. Unusable/conflicting placements degrade to clickable
end markers instead of dropping the translation or link. Turning alignment off
avoids its calls but can lose inline placement. A full translation run can retry
fallback annotations; export alone does not.

Navigation follows saved chapter boundaries. A TOC fragment missing from a uniquely
targeted resource can still establish a boundary at that resource's start.
Generated EPUBs preserve stored heading hierarchy, but inputs without EPUB templates
are rebuilt rather than given the original book's layout.

The optional “About this translation” page is enabled by default for EPUB only.
Simplified Chinese targets use the Chinese page; other targets currently use the
English fallback. Disable it with `output.about_page: false`.

<a id="html"></a>

## HTML and XHTML

HTML import translates consecutive inline prose together, including direct text in
layout containers, lists, tables and controls. Block boundaries, media, `<br>` and
protected subtrees separate translation regions. Links and styled spans are
represented by source ranges; models do not rewrite their URLs or structural
attributes.

Human-readable attributes such as `alt`, `title`, `placeholder` and descriptive
ARIA labels use separate plain-text segments. They are restored to their DOM
destinations, not appended as body paragraphs in TXT, Markdown or DOCX.
Scripts, styles, templates, code, formulas, hidden/opted-out subtrees and editable
content are not translated. Input drafts are not treated as prose.

HTML-to-HTML export backfills the **complete original file**, preserving its doctype,
head metadata, global CSS, shell attributes and layout. Saved chapter groupings do
not replace the source document. Export requires matching original content and
segment sources before replacing an output; it does not rewrite the original or
paid translation state.

Inline ranges use validated annotation placements. Crossed/overlapping sibling
ranges are rejected rather than guessed. Failed style placements leave text intact;
an unaligned ordinary link survives at the paragraph end with its source label.
Partial spans can add alignment calls; whole-source wrappers do not need them.

HTML exports are single `.html` files:

- Existing data URIs remain embedded.
- Readable local image/media resources from HTML sources are embedded as data URIs.
- EPUB-to-HTML embeds media from the EPUB container only. Missing references do not
  fall back to files beside the EPUB or in the working directory.
- Remote resources are not downloaded. Linked stylesheets and CSS `url(...)`
  resources remain references, even when they point to local files.

This is static-file adaptation, not live website translation or a universal offline
page packager. Wenyi does not execute JavaScript, calculate external stylesheet
visibility, inspect live shadow DOM or fetch remote assets. Preserved active content
is not sanitized into a security-safe document; treat exported HTML as you would its
source.

<a id="markdown-and-text"></a>

## Markdown, text and FB2

Markdown ATX headings (`#` through `######`, followed by whitespace and title text)
start chapters and retain their levels. Blank lines separate body paragraphs.
Recognized Japanese chapter labels also form boundaries; without recognized headings,
the reader uses one chapter. Plain-text contents lines with page numbers stay body
text.

Stored heading levels feed generated EPUB headings/navigation and DOCX outlines.
Navigation nests a heading beneath the nearest preceding lower-level heading;
skipped levels do not invent entries. TXT/Markdown output reconstructs headings
and paragraphs, not the source file's exact whitespace or a complete Markdown AST.
Do not expect arbitrary fenced blocks, inline syntax, tables or embedded HTML to
have the protection of the dedicated HTML importer.

FB2 import reads nested sections, headings, prose and supported verse/citation
content, retaining image references for reconstruction. Dedicated `notes` bodies
are skipped. It generates a new output book rather than backfilling an FB2 template;
there is no FB2 writer.

## DOCX

DOCX follows the same preparation, glossary, polishing, Review and resume workflow
as other book inputs. It is a reconstruction of supported document structures,
not an exact Word package round trip.

- Heading/outline levels are recorded; level-one headings split chapters.
- Simple tables are rebuilt cell-by-cell. Merged cells and nested tables are not
  supported.
- Automatic lists are reconstructed as numbered/bulleted groups, restarting by
  source list identity. Visible numbering already present in text is not added again.
- Recorded bold, italic, underline, color, size, paragraph alignment and shading
  are retained where supported.
- Uniform paragraph styles need no extra alignment call. Meaningful mixed-style
  spans can require model alignment; a failed span falls back proportionally rather
  than discarding the entire paragraph. Font/size-only splits do not trigger it.
- Target-language font policy applies to translations, not untouched source or
  bilingual source copies. Chinese translations use the built-in Song font policy;
  actual display still depends on available fonts.

Heading hierarchy supports Word's navigation pane. Do not assume preservation of
complex table layout, dynamic Word fields or exact pagination. Check a representative
export before processing a layout-sensitive document.

<a id="pdf"></a>

## PDF: choose the backend first

| | MinerU | BabelDOC |
| --- | --- | --- |
| Default | Yes | Opt-in |
| Primary goal | Extract/reflow text through converted HTML | Preserve original page layout where possible |
| Service boundary | External conversion service | External AGPL bridge, separate repository/process, HTTP only |
| Scanned image-only input | Use this path or OCR first | Requires an extractable text layer |
| Default CLI output | EPUB | PDF |
| PDF export | Not available | Requires the saved bridge session |

### MinerU

The first conversion requires `MINERU_API_KEY` in the CLI environment.
Converted HTML is cached under
`state/<book>/targets/<target-language>/source/<source-sha256>/converted.html`.
Matching retries reuse conversion rather than treating another PDF with the same
filename as equivalent. Review extraction quality before committing to a long
translation; OCR/conversion quality limits all later stages.

MinerU state supports reflow exports, not PDF reconstruction. Some parsed units may
intentionally have empty completed translations; they are not automatically pending.
Changing configuration to BabelDOC later cannot add the missing layout session.

### BabelDOC

Run the separate `wenyi-babeldoc-bridge` service and select
`pipeline.pdf_backend: babeldoc`. Its default URL is `http://127.0.0.1:8765`;
`pipeline.babeldoc_pages` optionally selects 1-based pages. Wenyi does not import
BabelDOC or include its AGPL runtime in Core/CLI; extraction and fillback use HTTP.
Review the external service's deployment and licensing requirements separately.

Before contacting the bridge, Wenyi checks selected pages for image-only content.
If no extractable text layer is found, use MinerU or run OCR first. Chapter grouping
uses PDF bookmarks, with one chapter when no outline is available.

For translation continuity, only prose units supply preceding/following
prose context. Explicit headings bound sections; decorations, tables, captions and
unknown layout roles do not contaminate that context. Non-prose batches omit the
prose synopsis/digest context but remain translation units. Layout misclassification
can therefore reduce useful context; this is not table row/column reconstruction.

PDF re-export sends saved text to the bridge. Missing, empty
or whitespace-only targets fall back to original paragraph text for that export,
without changing saved targets. Layout boxes and role labels are omitted by default.
Fonts, text expansion and the external engine still limit layout fidelity.

With an annotation-capable bridge, Wenyi uses its shared annotation aligner to
locate PDF formulas, superscript/citation markers, mixed styles and linked text in
the completed translation. The bridge restores original formula/marker objects
and styles, and rebuilds URI and internal page links against the typeset glyphs.
Resolved named destinations, commonly used by citation links, are supported.
Internal links to pages excluded from the export are omitted; other PDF action
types are not reconstructed. Formula recognition still depends on BabelDOC.

Keep `pipeline.annotation_alignment` enabled to locate translated PDF annotations.
Export applies valid items independently: successful links, styles, formulas and
markers remain even when another item in the same paragraph fails. Missing,
invalid, stale or fallback positions are omitted without guessing locations,
blocking export or replacing the translated body with source text. Conflicting
formulas or incompatible styles omit only the conflicting items.

Export itself makes no model calls. Resume translation refreshes missing/stale
alignment after editing saved targets, but does not automatically retry unchanged
PDF fallback items in a repair pass. CLI logs contain per-kind placement-validation
and application rates, skipped counts and reasons; the bridge session's
`annotation_stats.json` includes per-item IDs. These rates measure validation and
application, not proven semantic accuracy, and separate translated text from
source-identity placements. Update and restart the bridge for best-effort export.
Import the PDF again to add annotation metadata to older text-only projects.

**Keep both Wenyi state and the bridge session.** Set `WENYI_BABELDOC_STATE_DIR` on
the bridge to persistent storage for long jobs. Its frozen extraction session can
be restored after restart only while the session directory and the same
Python/BabelDOC versions remain available. A temporary directory can disappear after
reboot; Wenyi's translated chapters alone cannot recreate that PDF layout session.

For a bilingual PDF, Wenyi combines each original page with the corresponding
bridge-produced translated page, forming one wider PDF page per pair. The
[bilingual order](#naming-and-bilingual-output) places the first version on the
**left** and the other on the **right**. Both retain their original physical size, visible crop and
orientation; unequal heights align at the top. Text and vector content are not
rasterized. Source-style options do not affect this layout.
Internal link destinations are remapped to the corresponding half of the output
spread, including noncontiguous page selections.

Keep the original PDF as well as the bridge session. Pairing uses the page
selection saved at import, not current settings. A changed source, unreadable PDF
or mismatched page count fails rather than guessing a pairing or replacing an
existing export with a partial file. Monolingual PDF export is unchanged.

## Source snapshots

A source snapshot records parsed chapters and source paragraphs before AI preparation.
It binds the source hash and parsing settings, supports read-only chapter preview,
and is reused only when those inputs match. It does not contain an initialized
translation manifest or make partially staged targets editable.

Keep the original input alongside saved state. EPUB and HTML backfill depend on
their original templates/resources; HTML export verifies extracted source segments
as well as the recorded hash. Saved source previews are not a substitute for all
original assets. A new input with the same filename needs separate translation state.

<a id="export-snapshots"></a>

## Consistent export snapshots

Standalone `assemble` captures the saved book consistently, then renders that snapshot.
It can run while another
workflow translates; later batches appear only in a later export.

Re-export makes no translation-model calls and does not rerun preparation, Review,
polishing or annotation repair. BabelDOC PDF export still contacts its external
bridge. Export-only punctuation normalization changes an in-memory copy for
Simplified Chinese targets without changing saved translations or losing the position
of annotations and styles. Other targets skip that normalization.

Changing only output format, bilingual order, source styling or export font/punctuation
policy therefore needs a fresh export, not a fresh paid translation.
