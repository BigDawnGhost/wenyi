# 04 · Establish shared markup ownership before splitting EPUB I/O

[Index](README.md) · [简体中文](../zh/refactoring/04-epub-markup.md)

Status: implemented; baseline: `7471256`.

Implemented: shared markup contracts, ruby, anchors, annotations and segmentation now serve both input and output. EPUB package/layout, navigation/resources/presentation and HTML inline/bilingual rendering have separate owners. Reader coordination is 120 lines, EPUB writer 345 and HTML renderer 226; anchor identities, template validation and export paths are unchanged.

## Evidence

[`epub_reader.py`](../../trans_novel/ingest/epub_reader.py) has 1,561 lines, [`epub_writer.py`](../../trans_novel/assemble/epub_writer.py) 858, and [`html_renderer.py`](../../trans_novel/assemble/html_renderer.py) 747. There is a real shared boundary: HTML input calls `annotate_epub_resource`; EPUB output calls that function and the private `_fragment_anchor_map` to rebuild templates; HTML rendering also imports the fragment helper. Translation imports `strip_ruby_markers` from the EPUB reader.

The reader currently combines archive/OPF access, DOM annotation, inline/ruby handling, note-context collection and logical chapter construction. Import-time lazy loading avoids eager dependencies, but does not give this shared logic a clear owner.

## Proposed layout

| Module | Contents and consumers |
| --- | --- |
| `markup/contracts.py` | Annotation/inline metadata names and pure types shared by ingestion and rendering. Preserve serialized keys. |
| `markup/anchors.py` | Fragment indexing and deterministic anchor utilities; used by ingest and assemble. |
| `markup/annotations.py` | DOM note-marker/range recognition and its local semantic helpers. |
| `markup/segments.py` | Translation-target selection, text/inline extraction, resource annotation; accepts markup plus resource identity, never opens a book. |
| `markup/ruby.py` | Ruby pronunciation marker definitions and stripping; usable without importing an EPUB reader. |
| `ingest/epub_package.py` | ZIP/OPF/spine and declared-resource discovery; no translation or rendering. |
| `ingest/epub_layout.py` | Map physical resources/TOC into logical chapters and collect cross-resource note contexts. Use the existing `epub_chapters.py` policy registry. |
| `ingest/epub_reader.py` | `peek_epub_title` and `read_epub` coordination. |
| `assemble/epub_navigation.py` | NAV/NCX title backfill and OPF metadata rewrite. |
| `assemble/epub_resources.py` | Reconstruct and validate physical resources against saved segments, then render once per resource. |
| `assemble/html_inline.py`, `assemble/html_bilingual.py` | Inline/annotation restoration and source-side anchor/link handling respectively. |
| Existing writers/renderers | Retain format coordination and call the extracted modules. |

`markup/` is a deterministic document-processing layer: it may use BeautifulSoup and ingest's pure models, but not archive readers, stores, pipeline services, LLMs or output writers. Keep all collaborators' imports directed toward this layer. Do not create a generic plugin registry for DOM rules in this extraction.

## Identity and rendering contracts

- Physical resource identity determines `tn{resource_index}_{segment_index}` anchors. Logical chapter splits must not renumber them. Keep `Segment.index`, `anchor`, `cont`, `resource_href`, TOC entry IDs and raw href semantics.
- A logical chapter may span XHTML files; multiple logical chapters may share one file. Reconstruct and write each physical file once, retaining all translations.
- The original EPUB remains authoritative for templates and inline layout. Do not start storing complete XHTML per logical chapter or relax current source/anchor mismatch checks.
- Preserve auxiliary non-spine note contexts without promoting them into formal chapters. Preserve point/range notes, backlink exclusion, ruby hints, processing instructions, images, line breaks and continuation merging.
- In bilingual output, source links stay on source-side anchors, target links stay on target-side anchors, including across resources. Build book-wide source link mappings before rendering individual files.
- Keep original template EPUB, generated EPUB from chapters, and generated EPUB from HTML templates as distinct export paths. All continue reading consistent export snapshots.

## Incremental implementation

First freeze physical-resource annotation results using small synthetic fixtures. Move shared constants/ruby, then anchor utilities, then DOM annotation and segmentation with identical traversal order. Update HTML input and both renderers to import this public shared layer; remove old private helper aliases.

Next move package reading and logical layout, retaining `epub_toc.py` and `epub_chapters.py` rather than replacing their existing split. Finally separate navigation, resource rebuild and HTML restoration. Do not change chapter-split policies or visual output in the same PR.

## Acceptance

Use `tests/test_ingest.py`, `tests/test_assemble.py`, `tests/test_bilingual.py`, `tests/test_annotation_aligner.py` and `tests/test_i18n.py`. Existing fixtures cover cross-spine notes, non-spine notes, malformed secondary TOC, nested fragments, ruby, pagebreak instructions and cross-file bilingual links.

Compare segment identities/source text/metadata, chapter and TOC mapping, normalized XHTML DOMs, OPF/NAV/NCX, and unchanged binary resources. Add an import boundary check preventing `markup` from importing workflow/storage modules and preventing writers from importing reader-private helpers. Check template and generated EPUB, single/bilingual output, explicit `--out`, default paths and concurrent snapshot export. Run the full suite after integrating the shared layer.

The benefit is one authoritative annotation path for ingestion and export. It is not a redesign of EPUB semantics or a promise to fix every source formatting issue.
