# Module responsibilities

[简体中文](zh/architecture.md) · [Pipeline](pipeline.md) · [Web development](web.md)

This is a map of the implemented boundaries, replacing the completed refactoring plans.
Core paths below are relative to `packages/core/wenyi_core/`.

| Area | Owner |
|---|---|
| Entry points | `packages/cli/wenyi_cli/cli.py` assembles the CLI; `commands/` registers commands with an invocation context. Web API and workers live in `apps/api/wenyi_api/`. |
| Pipeline routing | `pipeline/orchestrator.py` assembles services and owns routing and lock scopes; domain work stays in the services. |
| Translation | `pipeline/translation.py` coordinates translation; `translation_batch.py` returns explicit batch results; `title_translation.py` handles titles. |
| Whole-book review | `pipeline/review_workflow.py` coordinates sessions; `review_checkpoint.py`, `review_rounds.py`, `review_chunks.py`, and `review_results.py` separate recovery, decisions, execution, and result handling. |
| Autofix | `pipeline/review_autofix.py` coordinates the separate publication service; `autofix_candidates.py`, `autofix_plan.py`, and `autofix_publish.py` separate candidates, the recoverable index, and writes to formal translations. |
| Review agents | `agents/review_*.py` owns model interactions; `review/` provides shared evidence, types, and run artifacts without depending on pipeline orchestration. |
| EPUB / HTML | `markup/` owns shared deterministic anchors, annotations, ruby and segment handling; readers and writers remain in `ingest/` and `assemble/`. |
| DOCX | `document_styles/docx.py` owns pure style policy; DOCX readers and writers own parsing and document emission. |
| Persistence | Domain services use `storage/protocol.py`. `storage/file.py` adapts local RunStore/SQLite; Web injects `apps/api/wenyi_api/storage_pg.py` for PostgreSQL state. |

Keep Core independent of CLI and Web frameworks. Agents must not depend on the pipeline
or concrete state stores. Shared markup and style policy must not depend on LLMs or a
particular writer. SRT remains an independent lightweight path; BabelDOC remains an
external HTTP service.

Initialization commits the manifest last; publication uses a recoverable index before
changing formal targets. Stable segment identities, consistent export snapshots, and
once-only usage accounting remain contracts across storage backends. See the
[repository guide](../AGENTS.md) for the full constraints and required verification.
