# P02 · Build long-form quality benchmarks and evaluate complete prescan coverage

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p02-long-form-quality-evaluation.md)

Type: proposed roadmap, not implemented; dependencies and order in index · 2026-09-05

## Baseline and evidence

FakeClient tests validate workflow, alignment, and recovery, not literary quality. `digest_chapter()` sends only `source_text[:8000]`, excluding the end of long chapters from that digest request. Whole-book understanding needs measurable coverage. Evaluate complete prescan before changing defaults.

## Goal and implementation

Create reproducible quality/cost baselines for prompt, terminology, context, polish, and Review changes. Start with authored offline cases for chapter endings, cross-chapter pronouns, aliases, annotations, long segments, and false positives. Add a public-domain corpus of at least 50,000 words, matching the explicit English CONTRIBUTING requirement, with provenance and hashes. Compare prefix sampling with ordered chunk-and-reduce digests and resumable chunk checkpoints.

## Evaluation

Measure segment completeness, term consistency, character/pronoun errors, Review precision, newly introduced Autofix errors, blind human ratings, and tokens/time per 10,000 source characters. Freeze corpus/config/model/prompt identities and sample locations. Human review and model scoring are complementary; the same reviewer's clean result is not sufficient evidence. Report variation from repeated samples.

## Acceptance and sequencing

Add held-out-book evaluations for P06 retrieval/hard negatives, P07 desired consistency/incorrect unification, P08 sample coverage/character voice, and P09 coreference/relationship/age evidence and justified abstention. Split by book or series to prevent repeated-sentence leakage. Report decision coverage and error together so neither reuse-everything nor unknown-everywhere can earn misleading scores. Compare each capability independently and in combination to separate retrieval, evidence, and generation effects. See P06–P09 in the index.

Publish the benchmark specification, commands, and before/after report. Every long-chapter region must reach a chunk request, with exact coverage and resume support. Require long-form comparisons before major semantic changes become defaults. Depend on F05/F06 and coordinate with P03. Estimate 5–8 days for offline infrastructure; real evaluations have separate time/cost. This audit ran no paid models or private examples. Complete coverage increases calls; an explicitly labeled sampling mode may remain useful.

## Repository evidence

- [trans_novel/agents/synopsis.py:20](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/synopsis.py#L20)
- [trans_novel/agents/prompts.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L1)
- [trans_novel/pipeline/preparation.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L301)
- [tests/fake_llm.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/fake_llm.py#L1)
- [CONTRIBUTING.md:15](../../../CONTRIBUTING.md#L15)
