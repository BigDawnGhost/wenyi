# P01 · Version persistent state and provide verifiable recovery tools

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p01-state-evolution-and-recovery.md)

Type: proposed roadmap, not implemented; dependencies and order in index · 2026-09-05

## Baseline and scope

Keep source SHA-256, manifest-last initialization, atomic JSON, and existing locks. Autofix already versions its index, but ordinary book/SRT manifests lack a shared schema-evolution contract. Legacy state missing source identity is rejected with a rebuild message. This is a roadmap item, not a claim that current state is generally corrupt.

## Goal

Provide offline, nondestructive diagnosis of completeness, resumability, and cache invalidation after upgrades. Track content identity, storage schema, and model-semantic versions separately.

## Implementation

Define artifact versions, compatibility, and minimal schemas while retaining book/SRT domain models. Add a read-only `state check` or `doctor` for chapter/manifest consistency, source identity, pending Autofix, dependencies, and usage reconciliation. Migrate into a new directory with a change report and validation before explicit switching; refuse compatibility guesses.

## Recovery and accounting

Register P06–P09 vector indexes, translation memory, style evidence, and relationships as versioned derived artifacts. Distinguish source-rebuildable caches from human decisions and derivation lineage that must be preserved. Track relevant source/model/splitting/style/relationship dependencies separately. Diagnosis should identify the invalid layer and reason; rebuilding vectors must not delete formal targets or human acceptance.

Associate usage increments with commit identities. Use fault injection around persistence/accounting boundaries before choosing a journal protocol. A process-local cumulative snapshot is not an idempotent transaction.

## Acceptance and sequencing

Test legacy/current/unknown versions, damaged chapters, missing identity, partial initialization, and pending publication. Diagnosis preserves formal bytes; migration is repeatable and retains stable segment/format identities. Test duplicate increments and interrupted writes. Depend on F01/F05/F06/F09 contracts. Estimate 3–5 days for diagnosis, then 5–10 for migration/accounting. New-directory migration costs disk; missing source files limit what can be verified.

## Repository evidence

- [trans_novel/pipeline/runstore.py:264](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/runstore.py#L264)
- [trans_novel/pipeline/runstore.py:322](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/runstore.py#L322)
- [trans_novel/review/run_store.py:389](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/run_store.py#L389)
- [trans_novel/pipeline/review_autofix.py:680](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L680)
- [trans_novel/pipeline/metrics.py:118](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/metrics.py#L118)
