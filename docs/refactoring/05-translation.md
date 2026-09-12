# 05 · Extract title translation and make batch ownership explicit

[Index](README.md) · [简体中文](../zh/refactoring/05-translation.md)

Status: proposed; medium priority. Title extraction is a suitable first slice; batch recovery has higher risk. Baseline: `7471256`.

## Evidence

[`translation.py`](../../trans_novel/pipeline/translation.py) has 791 lines. `translate_titles()` at line 488 spans 258 lines—roughly a third of the file—and combines continuation merging, heading/TOC reuse, pending-title batching, prompts, model output validation and manifest checkpoints. `translate_chapter()` at line 197 spans 219 lines and coordinates body batches, context, glossary recovery, annotations, styles and chapter completion.

These are two different workflows. Keep chapter translation sequential because later batches depend on earlier targets; file decomposition is not authorization to parallelize it or introduce automatic repeated-sentence reuse.

## Proposed boundary

| Module | Responsibility |
| --- | --- |
| `pipeline/title_translation.py` | `TitleTranslationService`: build heading/TOC reuse plans, identify pending entries, commit validated title batches and synchronize chapter titles. |
| `agents/title_translator.py` | Render existing title prompts, call `translation.title`, validate count and strings, return titles without accessing a manifest/store. |
| `pipeline/translation_batch.py` | Typed `BatchPlan` / `BatchResult`, resume-aware batch selection, and a narrow translate/polish executor. |
| `pipeline/translation.py` | Own serial chapter iteration, applying results to chapters, commit order, rolling context, glossary checkpoints and completion. |

A `BatchPlan` contains the chapter identity, absolute text-segment positions, sources, term snapshot, immutable rendered context, synopsis/digest, annotation context slice and the immediate following source segment. A `BatchResult` contains aligned targets and pre-polish values. Move the existing `target_before_polish` mutation into one explicit result-application point; do not change which values are ultimately persisted.

Do not hide store access, mutation and model work inside a general batch callback. The main service must still show when targets, alignment, context and terms become durable. Use explicit collaborators instead of passing the whole runtime into every pure helper.

## Required behavior

Title translation preserves original book titles and stable TOC/anchor identity. Reuse a complete translated heading only under the current source-match rules, including continuation slices; never merge unrelated locations just because strings happen to match. Preserve the existing 40-title / 4,000-character batch boundaries, prompt templates, operation ID, response validation and batch-level manifest saves.

For newly translated body batches, preserve the existing sequence: assign/save targets → annotation and DOCX alignment → rebuild rolling context → event/progress → glossary extraction/checkpoint and translation-history update. Chapter-level glossary extraction and final chapter status remain afterward. Completed batches restore context and finish missing alignment/glossary work without retranslating their text.

The one following source segment remains read-only reference material for translation and polishing. It must not be counted in output, persisted as a translated batch item, or appended to rolling target context. Preserve same-chapter boundaries and singleton-fallback neighbor selection.

## Slices and tests

1. Extract title reuse/planning as pure functions, then the title agent and title service. Keep current call order from `TranslationService.run`.
2. Add batch input/output types and isolate translate/polish calculation while keeping persistence in `translate_chapter`.
3. Extract resume selection only where tests show an independently useful boundary. Avoid a separate module for every five-line helper.

Use `tests/test_assemble.py` for translated title counts, heading reuse and shared-XHTML TOC; `tests/test_orchestrator.py` for partial batches, changed batch budgets, glossary checkpoints and skipped snapshots; `tests/test_translation_context.py` and `tests/test_translator.py` for following-source context. Also run glossary, annotation, DOCX, timing and architecture tests; run the full suite for batch changes.

Inject interruption after target save and before glossary checkpoint; resume must finish bookkeeping without another translation call. Compare operation IDs/messages and saved chapter/manifest data with the baseline, including pre-polish targets. Confirm title-only extraction does not invalidate completed body batches.

Future translation memory and evidence-backed style analysis can supply explicit batch inputs after separate designs. Exact or semantic matching must include context/provenance policy; this refactor does not make identical source text an unconditional cache key or infer older/younger siblings without evidence.
