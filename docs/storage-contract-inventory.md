# Storage contract inventory (batches D2-A-04a / 04b / 05)

Measured on `refactor/storage-contract`, whose parent is `a6d8f1a5`. Regenerate the numbers
with the method described at the end; they drift as the pipeline moves.

## What was measured

`Storage` declares **52** names: 6 artifact operations (`ArtifactStorage`), 3 path
properties and 43 state methods. Production call sites on those names total **626**;
tests add **1960**.

`FileStorage` implemented ~20 of them and forwarded the rest to its internal `RunStore`
through `def __getattr__(self, name)`, so the port was satisfied by a catch-all rather than
by a declaration. The 52-name port looked complete while any `RunStore` attribute — public
or private — answered for it.

## The three path properties

| Property | Production call sites | Test call sites | Verdict |
|---|---|---|---|
| `run_dir` | 59 | 83 | 4 logical, 55 display/identity |
| `source_dir` | 2 | 3 | **all logical** |
| `reviews_dir` | 4 | 7 | **all logical** |

Counted over `packages/` and `apps/`, excluding the storage implementations themselves.
The task brief's "278 call points" counts every textual occurrence of the three names,
including `review/run_store.py` and `run_store.py` internals that are not `Storage` calls;
the reachable production surface is 65.

### Classification rules

- **Display**: the string reaches a console, a log field or a report body and nothing
  reads it back. `console.print(f"State directory: {store.run_dir}")` stays.
- **Logical**: the value is used to build a filesystem location that code then opens,
  joins or passes to a reader. These must move to a port operation.

### `run_dir` — 55 display, 4 logical

Display (unchanged in this batch): `commands/workflows.py:55,175,244`,
`commands/inspection.py:88,177`, `commands/run_report.py:45,50`, the
`run_resumed`/`run_initialized` event fields in `pipeline/preparation.py:132,167,245`, and
the `review_dir`/`run_dir` fields reported by `pipeline/orchestrator.py:166,313` and
`pipeline/review_workflow.py:213,375,382,396,412` — every one of those is read straight out
of `ReviewRunStore`/`ReviewOutcome`, not from `Storage`.

Logical (4):

- `assemble/export_view.py:72` passes `store.run_dir` into `RunStore(..., create=False)` to
  build an export view.
- `pipeline/review_autofix.py:94,113` and `pipeline/review_workflow.py:196` pass
  `store.run_dir` into `ReviewRunStore.open_existing` / `find_resumable`.

### `source_dir` — 2, both logical

- `pipeline/input_preparation.py:80` hands it to `load_document(cache_dir=...)`.
- `assemble/html_resources.py:176` hands it to `pdf_cache_html_path`.

### `reviews_dir` — 4, all logical

- `pipeline/review_autofix.py:59` joins it with a review id.
- `run_store.py:244,499,502` already live inside the storage layer; the last two are the
  implementation of `load_latest_review_result`.

## Why the paths were not removed in this batch

All four logical sites feed `review/` and `assemble/`, which are filesystem-freeze layers
with their own test suites (`test_review_session.py`, `test_review_run_store.py`,
`test_docx_bilingual.py`). Replacing the path with a run identity means changing the
`ReviewRunStore` constructor contract, `find_resumable` discovery, the snapshot constructor
in `export_view.py` and the `PostgresStorage` branch that has no directory at all. That is a
behaviour-changing refactor, and this batch is a contract-only change; mixing them would
make any regression unattributable. The gap is therefore **declared** in
`storage/protocol.py` and **pinned** by
`test_architecture_boundaries.py::test_file_storage_has_no_catch_all_attribute_forwarding`.

## What `__getattr__` was actually forwarding

Measured two ways.

**Static**: `RunStore` members that `FileStorage` did not define — 13, all private helpers
or path getters:

```text
_exists  _file_lock  _listdir  _read_json  _remove  _usage_commit_path
_validate_source_identity  _write_json  analysis_path  annotation_contexts_path
ensure_dirs  event_lock  sweep_temporary_files
```

**Dynamic**: instrumenting `__getattr__` and running the full suite (3149 collected) recorded
**0** forwarded lookups. Production and tests only ever called names `FileStorage` already
declared.

Together: the 13 are a latent hole, not a live dependency. They stay unreachable after
`__getattr__` was deleted, which is why the removal is behaviour-neutral. The one member that
tests did reach through the instance, `sweep_temporary_files`, was an explicit forwarder
already.

## The second, larger gap: six undeclared path attributes

Deleting `__getattr__` exposed names the port never declared but `FileStorage` answers:

| Member | Production callers | Test callers |
|---|---|---|
| `chapter_path(ci)` | 0 | ~20 (`test_review_session`, `test_orchestrator`, `test_i18n` …) |
| `manifest_path` | 0 | ~10 |
| `usage_path` | 0 | 3 |
| `initialization_path` | 0 | 3 |
| `glossary_path` | 4 (CLI `GlossaryStore(store.glossary_path)`) | several |
| `event_log_path` | 1 (`commands/run_report._read_events`) | several |

They are listed in `FILE_STORAGE_PATH_GAPS` and asserted as an exact set, so the leak cannot
grow silently. Closing it means adding a glossary-handle operation and an event-read
operation to the port; tests that read raw bytes of state files need a different assertion
strategy, which is a separate change.

## Contract changes landed in this batch

- `FileStorage.__getattr__` deleted; nothing replaced it, because nothing needed it.
- `Storage` documents that the three path properties are a **gap**, not the target contract.
- `storage/run_store.py` now owns `RunStore`, `ExportSnapshotStore`, `slugify`,
  `translation_run_dir` and `source_sha256`. `storage/` no longer imports `pipeline/`.
- `pipeline/runstore.py` is a forwarding shell over the new location.
- The backend reaches orchestration through `execution/ports.WorkflowRunner`;
  `execution/bindings.py` is the only module that names `Orchestrator`, and the B0
  exemption entry is gone.

## Regenerating the numbers

```bash
# 52 declarations
grep -c "    def " packages/core/wenyi_core/storage/protocol.py
# call sites per property
grep -rn "\.run_dir\b\|\.source_dir\b\|\.reviews_dir\b" packages apps --include=*.py
```

For the dynamic check, add a temporary trace to `FileStorage.__getattr__` that appends the
attribute name, run `PYTHONUTF8=1 uv run --no-sync pytest -q -n auto`, and confirm the trace
stays empty before deleting the instrumentation.
