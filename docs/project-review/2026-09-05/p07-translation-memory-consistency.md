# P07 · Use translation memory for repeated expressions in equivalent contexts

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p07-translation-memory-consistency.md)

Type: user-requested roadmap extension, not implemented; proposed interfaces and thresholds require evaluation · 2026-09-05

## Goal and scope

Keep repeated vows, catchphrases, and fixed narration consistent when meaning and context match, while retaining character, irony, and rhetorical variation. Existing historical evidence supports terminology decisions, not sentence-level translation memory. Do not put arbitrary sentences into the glossary.

## Three modes

Exact source plus confirmed compatible context may reuse accepted memory. Identical source with different speaker, reference, or pragmatics requires translation with suggestions. Vector-near matches remain reference-only by default. Exact lookup can precede P06. Keep original and normalized keys separate; normalization must preserve meaningful negation, numbers, names, case, and quotation differences.

## Examples and gates

A character repeating a vow, “I will return.”, can remain consistent. Sincere versus ironic “You are right.” may differ. “My brother is here.” uttered by different people has different relational anchors. Check entities/references, polarity/quantity/tense, pragmatics, and style scope; unknown critical conditions prevent automatic reuse.

## Records and alignment

Store source/target text and hashes, stable refs and sentence spans, speaker/reference candidates, style/relationship versions, acceptance provenance, applicability, and derivation lineage. Earliest/most frequent model output is not automatically authoritative. Record human or specific review evidence. A source sentence may map to multiple target clauses; without confirmed alignment, suggest complete segments rather than splicing text or changing formal identities.

## Publication and invalidation

Constrain new-batch translation with accepted entries and verify actual adoption afterward. Completed-text scans produce suggestions and justified variants; read-only Review proposes complete replacements and enabled Autofix publishes. Revalidate locked phrases changed by polishing. Isolate memory per book initially. Revoked/revised entries mark dependent locations for review instead of silently rewriting them. Future cross-book reuse needs explicit style/provenance and permitted-source scope.

## Acceptance and sequencing

Start with exact keys, manual lock/unlock, and difference reports; then add contextual gates and P06 fuzzy candidates. Measure both desired consistency and incorrect unification of legitimate variants, plus meaning and literary quality. Test irony, different speakers, polysemy, negation/numbers, terminology changes, polishing, misalignment, and recovery. Failed cache/reference commits preserve formal targets; reuse must not invent LLM usage. Depend on F01/F05 and P01/P02; refine with P08/P09. Estimate 5–8 days for exact memory/reporting plus 5–10 for automatic gates and lineage invalidation.

## Repository integration points

- [trans_novel/pipeline/translation.py:133](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/translation.py#L133)
- [trans_novel/pipeline/translation.py:198](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/translation.py#L198)
- [trans_novel/pipeline/translation.py:738](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/translation.py#L738)
- [trans_novel/glossary/extractor.py:35](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/glossary/extractor.py#L35)
- [trans_novel/agents/translator.py:139](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/translator.py#L139)
- [trans_novel/pipeline/review_autofix.py:721](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L721)

Engineering estimates assume one contributor and exclude real model/embedding evaluation and long-form human review. Implementation must synchronize config models, built-in/root examples, bilingual docs, and CLI/config tests; default automated tests remain offline.
