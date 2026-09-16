# P08 · Strengthen style analysis with stratified samples and source evidence

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p08-evidence-backed-style-analysis.md)

Type: user-requested roadmap extension, not implemented; proposed interfaces and thresholds require evaluation · 2026-09-05

## Baseline and goal

Existing analysis includes genre, tone, narration, pacing, register, dialogue, and rhetoric, with beginning/middle/end samples. Those local passages produce a cleaned global brief without per-claim evidence, coverage, or counterexamples. Improve representativeness and preserve character differences.

## Sampling

Deterministically sample source positions across chapters, narration/dialogue, letters/diaries, viewpoints, and sufficiently represented characters; retain refs/ranges and selection reasons. Unknown speakers remain unknown. Embeddings may discover underrepresented groups, but P06 is not required for the first version. Keep language detection on unlabeled source. Analyze local samples before aggregation rather than only enlarging one prompt.

## Output and adjudication

Separate observations about source style from Chinese translation policy. Each dimension has book/chapter/narrator/character scope, supporting and counterexample refs, coverage, status (supported/uncertain/conflicted), and rationale. Provide actionable guidance and short examples. P09 owns factual relationships; style guesses must not become character facts. Aggregate commonality while preserving local differences. Leave unsupported dimensions unresolved. Repeated model agreement measures stability, not independent evidence; validate against held-out source, not model-polished translation.

## Integration and versions

Preserve manifest-last initialization for initial analysis. Later prescan/sample revisions create separate versions; check required preparation readiness and freeze a snapshot before translation. Translator, Polisher, and Fixer receive the same global/local versions. Queue new exceptions for explicit boundary updates instead of batchwise drift. Changed style versions invalidate relevant F05/P07 caches while leaving completed targets untouched by default.

## Acceptance and sequencing

Test short books, mixed viewpoints/characters, irony, register shifts, inserted letters, rare styles, and conflicts. Deterministic sampling, validated evidence refs, and resumable failed samples are mandatory. Compare evidence coverage, unsupported guidance, sample stability, and character voice through blind evaluation, checking homogenization and added ornament. Depend on P02/F05/F06 and P01/P03. Estimate 5–8 days for sampling/evidence plus 5–10 for layered guidance/evaluation infrastructure.

## Research boundary

Long-context position effects motivate evaluation of sample placement; they do not establish the same failure magnitude for Wenyi's configured models. This architecture remains a project proposal. [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/)

## Repository integration points

- [trans_novel/pipeline/preparation.py:274](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L274)
- [trans_novel/agents/analyzer.py:26](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/analyzer.py#L26)
- [trans_novel/agents/analyzer.py:89](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/analyzer.py#L89)
- [trans_novel/agents/prompts.py:39](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L39)
- [tests/test_preparation.py:13](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_preparation.py#L13)
- [trans_novel/pipeline/preparation.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L301)

Engineering estimates assume one contributor and exclude real model/embedding evaluation and long-form human review. Implementation must synchronize config models, built-in/root examples, bilingual docs, and CLI/config tests; default automated tests remain offline.
