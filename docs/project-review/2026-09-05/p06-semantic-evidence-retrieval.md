# P06 · Introduce semantic retrieval with traceable whole-book evidence

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p06-semantic-evidence-retrieval.md)

Type: user-requested roadmap extension, not implemented; proposed interfaces and thresholds require evaluation · 2026-09-05

## Goal and integration

Find paraphrased events, age/kinship clues, and comparable narration/dialogue without requiring identical keywords. Extend the existing evidence interface, which already provides stable refs, term/alias matches, context, and shadow provenance. Start in the book workflow; keep SRT independent.

## Retrieval versus decisions

Combine exact/lexical matching with vector candidates, scope/entity filters, and optional small-candidate reranking. Relevance scores do not establish sentence interchangeability, kinship truth, or translation correctness. Domain rules and evidence adjudication decide those questions. No match is not proof of absence: record search scope and coverage.

## Index and query contract

Keep formal segment indices, chapters, and anchors. Derived sentence/window entries carry source hash, segment ref, source-character range, split version, and content hash. Separate source evidence from accepted translation memory. Target edits invalidate target entries, not unchanged source vectors; Review shadows remain run-local. Return refs, excerpts/ranges, chapter/speaker candidates, match type, ranking score, truncation/coverage, and provenance; fetch full context by ref. Apply reliable entity constraints, but retain alternatives and widen search when identity is uncertain. Break ties in stable book order and treat quoted content as data.

## Implementation and recovery

Define embedding/retrieval interfaces, FakeEmbedding, and an exact-vector-scan baseline before choosing an approximate index or service from measured scale. Domain services own concurrency/budgets. Treat indexes as rebuildable caches keyed by model/version/dimension, normalization/distance, splitting, and source identity. Explicitly rotate generations when model identity cannot be pinned. Checkpoint batches and atomically switch only validated generations; never mix vector spaces. Use environment credentials and explicit local/remote configuration identifying text sent remotely, with P03 accounting.

## Acceptance and sequencing

First add budgeted read-only source retrieval to Review; then serve P09/P08 and P07 fuzzy candidates. Compare lexical, vector, and hybrid strategies on P02 labels using Recall@k, Precision@k, latency, size, and token cost. Test negation, numbers, namesakes, paraphrases, missing answers, and hard negatives. Calibrate on development data and report held-out books; no universal equivalence threshold. Depend on F05/F06 and P01/P02/P03. Estimate 5–8 days for a prototype plus 5–10 for incremental indexing/calibration, excluding model evaluation.

## Research boundary

Sentence embeddings have a primary research basis for similarity search. This does not establish Wenyi-specific retrieval quality; the hybrid architecture, contracts, and decision policy above are project proposals. [Sentence-BERT](https://aclanthology.org/D19-1410/)

## Repository integration points

- [trans_novel/review/evidence.py:34](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L34)
- [trans_novel/review/evidence.py:167](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L167)
- [trans_novel/review/evidence.py:356](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L356)
- [trans_novel/agents/prompts.py:125](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L125)
- [trans_novel/ingest/models.py:22](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/ingest/models.py#L22)

Engineering estimates assume one contributor and exclude real model/embedding evaluation and long-form human review. Implementation must synchronize config models, built-in/root examples, bilingual docs, and CLI/config tests; default automated tests remain offline.
