# Maintainability refactoring

[简体中文](../zh/refactoring/README.md)

Status: completed on 2026-09-14. All seven plans, slices A–F and the later body-batch/CLI-context work are implemented and committed separately on `refactor/maintainability`.

The inventory below is historical: it was reviewed on 2026-09-11–12 against local `dev`, commit `7471256`. Implementation started from `eef85d4`, retaining the subsequent recoverable Review error fix.

Integration with `dev` (`08956d4`) preserves token-based body/Review budgets, MinerU-only blank-output allowance and translation-conversation polishing in the extracted modules. The merged suite passes **790 tests and 49 subtests**, including seven new batch integration cases. Verification counts and line counts below describe the original refactor branch before this integration.

## Recommendation

Refactor the Review workflows and the shared EPUB markup boundary first in architectural priority. Begin implementation with the smaller CLI presentation and title-translation extractions to establish the process. Keep the existing thin Orchestrator and provider/operation registries. A long file alone is not sufficient reason to split it.

This review inspected production Python files, their import relationships, significant methods, and relevant tests. Line counts include comments and blank lines. Function lengths include nested functions; the figures are navigation aids, not complexity scores or performance measurements. No private books, state, output, or external services were used.

## Baseline inventory and decisions

| Baseline file | Lines | Largest function | Decision |
| --- | ---: | --- | --- |
| `pipeline/review_workflow.py` | 1,891 | `run_session`, 717 | High priority: session state, recovery, execution and reporting intersect. [01](01-review-workflow.md) |
| `ingest/epub_reader.py` | 1,561 | `_logical_chapters`, 206 | High priority: archive reading and reusable markup processing share one module. [04](04-epub-markup.md) |
| `agents/review_loop.py` | 1,006 | `_ActionLoop.run`, 300 | High priority: conversation replay, protocol and arbitration need distinct boundaries. [02](02-review-agents.md) |
| `cli.py` | 953 | `_translate_impl_or_raise`, 110 | Medium priority, useful first slice: presentation, validation and command dispatch. [06](06-cli.md) |
| `pipeline/review_autofix.py` | 922 | `_run`, 604 | High priority: separate candidate planning from indexed publication. [03](03-review-autofix.md) |
| `assemble/epub_writer.py` | 858 | `_render_epub_resources`, 139 | Refactor together with shared markup ownership. [04](04-epub-markup.md) |
| `pipeline/translation.py` | 791 | `translate_titles`, 258 | Medium priority: extract titles first, then make batch commit order explicit. [05](05-translation.md) |
| `assemble/html_renderer.py` | 747 | `_render_segments_html`, 120 | Separate inline restoration and bilingual links within the EPUB work. [04](04-epub-markup.md) |
| `assemble/docx_writer.py` | 737 | `_emit_chapter_blocks`, 131 | Medium priority: isolate style policy, Word emission and block assembly. [07](07-docx.md) |
| `pipeline/runstore.py` | 607 | `begin_initialization`, 49 | Defer a wholesale split: short cohesive storage methods; sensitive transaction boundaries. |
| `agents/annotation_aligner.py` | 526 | `_align_batch`, 107 | Defer; most complexity belongs to one alignment protocol. Revisit if another alignment strategy is introduced. |
| `review/run_store.py` | 516 | `find_resumable`, 56 | Keep persistence together; move its pure types out under proposal 02. |
| `ingest/docx_reader.py` | 515 | `read_docx`, 144 | Limited shared-policy extraction under 07; no reader rewrite. |
| `glossary/store.py` | 433 | `upsert_term`, 61 | Keep SQLite term/conflict operations cohesive. |
| `pipeline/orchestrator.py` | 404 | `_finish_steps_locked`, 82 | Keep the façade; do not turn it into a workflow/plugin framework. |
| `srt/translate.py` | 385 | `translate_srt`, 206 | Watchlist: distinguish windows, ordered merge and persistence when next changing SRT. Do not move it into the book pipeline. |

`config.py` (221 lines), `model_commands.py` (217), and the LLM modules are not the current large-file problem. `model_commands.register_model_commands` is mostly explicit command definitions; reuse its registration pattern instead of introducing dynamic command discovery.

## Rules for all proposals

- Separate pure decisions from I/O and mutable state ownership. Moving methods into mixins while sharing all of `self._runtime` does not achieve that separation.
- Keep `CLI → Orchestrator → domain services → agents / ingest / assemble / stores`. New modules must not depend on the façade; agents may depend on pure review types and contracts, not concrete stores.
- Preserve operation IDs, prompt bytes, request order, selected models, cache fingerprints, stable segment/anchor identities, on-disk schemas and commit order during extraction. Update internal imports and tests together; do not retain aliases or compatibility wrappers for old private APIs.
- A later schema, prompt or quality-policy change is a separate feature with explicit migration/evaluation where needed. Existing state correctness is an active contract, not a reason to add legacy branches.
- Preserve dedicated locks, manifest-last initialization, export snapshots, append/merge usage semantics, and one outer workflow timer. Moving code must not add timers around individual extracted services.
- Keep BabelDOC as an HTTP service. Introduce neither AGPL Python dependencies nor new network requirements.

## Completed implementation sequence

All slices are complete. Review agents now use pure contracts and replay state; whole-book Review separates checkpoints, scan/fix decisions, execution and result writing. Autofix keeps candidate generation separate from recoverable publication. EPUB/HTML share deterministic markup ownership; DOCX shares pure style policy. Title and body executors return explicit results, and command groups receive an invocation context.

The interruption tests also exposed and fixed three Review recovery defects: completed fixer calls could repeat, restored active patches could diverge from history records, and earlier scan counts could disappear from resumed summaries. The dedicated fix commit precedes the coordinator extraction.

| Slice | Scope | Dependency and acceptance gate |
| --- | --- | --- |
| A | 06: presentation extraction; 05: title planning/translation | Independent small changes; CLI behavior and title checkpoints unchanged. |
| B | 02: pure review contracts and conversation replay | Freeze current trace replay before changing the session coordinator. |
| C | 01: checkpoint codec, decisions, chunk execution, coordinator | Build on B; each move must preserve failure/resume traces. |
| D | 03: candidate planning and indexed publisher | Share established review types; keep publication a separate service. |
| E | 04: shared markup first, then EPUB reader/writer internals | Can proceed independently of B–D; do not overlap edits with title identity changes. |
| F | 07: DOCX style policy and emitter | Can proceed independently after shared-policy location is settled. |

Each slice can contain multiple small PRs. Avoid combining folder moves, schema changes and altered behavior in one PR. Priorities indicate maintenance risk, not a claim that current results are incorrect. Effort is dominated by regression coverage, especially for Review and EPUB; no unmeasured speedup is promised.

Suggested review signals: a coordinator should expose a readable sequence of stages; a helper module should have one owner and one reason to change; another feature should not need to import a private helper from a workflow. Use approximately 200–500 lines per cohesive implementation module and under 100 lines per coordinator as review prompts, not hard CI limits.

## Verification and completion

The original baseline passed **714 tests and 49 subtests**. Final validation passes **771 tests and 49 subtests**, Ruff lint/format checks and `git diff --check`. Focused Pyright checks for the new Review, Autofix, markup, document-style, batch and CLI boundaries report no errors or warnings.

Six synthetic EPUB input models and 18 template exports match the pre-extraction implementation in all internal resources, including XHTML/OPF/navigation and binary assets. Six DOCX combinations match in Word XML and resources. Tests cover Review/conversation/publication interruptions, usage recovery, glossary checkpoints, target/source identity, shared-resource backfill, snapshots and CLI entry-point behavior. These are offline equivalence checks, not a translation-quality benchmark or a measured speedup.

| Entry module | Baseline lines | Final lines |
| --- | ---: | ---: |
| `pipeline/review_workflow.py` | 1,891 | 419 |
| `agents/review_loop.py` | 1,006 | 265 |
| `pipeline/review_autofix.py` | 922 | 182 |
| `ingest/epub_reader.py` | 1,561 | 120 |
| `assemble/epub_writer.py` | 858 | 345 |
| `assemble/html_renderer.py` | 747 | 226 |
| `assemble/docx_writer.py` | 737 | 66 |
| `pipeline/translation.py` | 791 | 467 |
| `cli.py` | 953 | 43 |

Line reductions describe responsibility moves; the extracted implementation still exists in focused modules. No prompt, model route, state schema, output policy or workflow timing scope was changed. No private book/state/output files were included.

Before each implementation slice, retain or add behavior tests at its public boundary. Compare FakeClient operation/message sequences, structured outputs, stable IDs, persisted checkpoint content, usage totals and semantic event ordering; exclude timestamps, temporary paths and invocation IDs. Preserve request order where order is defined; concurrent calls need job-keyed comparison and deterministic merged output. Prefer structural EPUB/DOCX comparisons to ZIP byte equality.

Run affected tests, `test_architecture_boundaries.py`, `test_orchestrator_contract.py`, Ruff checks and `git diff --check`. Cross-module state changes require the full suite. Architecture checks now recursively inspect pipeline, agent, command, shared markup and document-style packages; pure Review contracts also cover Autofix records.

Do not use real LLMs or private samples for extraction tests. If prompts, context, termination policy or other translation behavior actually change, apply the public-domain evaluation requirement in `CONTRIBUTING.md` separately.

The [2026-09-05 Review proposal](../project-review/2026-09-05/p04-review-state-machine-refactor.md) is historical context. Proposals 01–03 refine it against the current code; old issue numbers are not automatically treated as still-unfixed prerequisites.
