# 03 · Separate Autofix candidate planning from indexed publication

[Index](README.md) · [简体中文](../zh/refactoring/03-review-autofix.md)

Status: implemented; baseline: `7471256`.

Implemented: pure publication records, deterministic candidate planning, isolated verification workers and indexed publication now have separate modules. The façade retains newest-run selection and usage-flush order. Five added regression cases cover interrupted index/chapter/alignment/result writes, repeated recovery, multiple changes at one location and conflicting external edits.

## Evidence

[`review_autofix.py`](../../trans_novel/pipeline/review_autofix.py) has 922 lines. `_run()` at line 128 spans 604 lines, combining existing-index checks, inference identity, shadow-change normalization, final issue verification, concurrent fixing, candidate records, location aggregation and publication. `_apply_index()` at line 743 handles actual chapter writes, alignment refresh and publication status.

The existing index-before-target-write design is valuable. Preserve it while making the planning/publishing boundary explicit. This is not a proposal to publish directly from a Review agent.

## Proposed modules and contracts

| Module | Responsibility |
| --- | --- |
| `review/autofix_models.py` | Pure `AutofixRecord`, `PublishLocation`, `AutofixPlan` types and validation; encode the current JSON shape. |
| `pipeline/autofix_candidates.py` | Normalize shadow changes, build an immutable effective-target view, group remaining issues, verify/fix groups, merge results by original position. |
| `pipeline/autofix_plan.py` | Convert candidate results into deterministic records/locations, compute hashes, save planning identity and the recoverable publication index. |
| `pipeline/autofix_publish.py` | Apply only an indexed plan, validate formal targets, refresh dependent alignments and finalize publication records. |
| `pipeline/review_autofix.py` | `ReviewAutofixService`: select the newest relevant run, route resume or planning, and retain usage-flush scopes. |

The publisher receives a validated plan, the formal chapter store, the review journal, and explicit annotation/DOCX alignment collaborators. It does not receive a candidate fixer or perform another whole-book Review. Planning receives an immutable chapter view and cannot save formal chapters. Keep pools in candidate execution, not in the façade or publisher.

## Publication contract

1. Resolve the newest valid Review and any existing index before new candidate/model work; do not publish an older leftover index behind a newer Review.
2. Record and validate planning inference identity. Preserve the current refusal to reuse a plan after planning-model changes.
3. Save the complete `autofix/index.json` before modifying any formal target. Preserve record IDs, location order, before/after text and hashes.
4. At each location: current equals indexed target → reuse; current matches indexed original and hash → apply; otherwise → conflict/failure without overwriting it.
5. Save changed chapter text, refresh unfinished alignment, then checkpoint index progress. Resume must tolerate interruption between any two of these operations.
6. Finalize completed/partial outcomes and merge usage once. Do not add segment history fields or modify manifest/glossary.

Recovering an index skips candidate generation and issue reverification. It does **not** imply that every resume is model-free: unfinished annotation or mixed DOCX style alignment may invoke the existing model-backed services. Preserve that distinction in tests and documentation.

## Implementation and acceptance

Extract plan types and pure aggregation first, then the publisher with its existing order, then candidate execution. Only after those moves should `_run()` become a short coordinator. Apply proposals 01–02's types where needed, without moving publication into the shadow-review engine.

Existing tests in `tests/test_review_autofix.py` cover field preservation, final verification, failed fixing, pending-index replay and rejection of older publication indices. Extend them with failures after index save, after chapter save, during alignment and before final result save. Retry each case twice: final targets, record statuses, alignment status and usage must match an uninterrupted run. Include conflicting externally edited targets, multiple records for one location, and mixed successful/failed locations.

Run Review Autofix, annotation alignment, DOCX, timing, routing/resume and façade tests, then the full suite. Preserve the current disabled-Autofix and pending-publication routing behavior as a characterization baseline; changes to whether pending work may publish are a separate user-visible policy decision, not part of moving these functions.
