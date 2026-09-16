# P09 · Resolve kinship and references through character-linked evidence

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p09-character-kinship-evidence.md)

Type: user-requested roadmap extension, not implemented; proposed interfaces and thresholds require evaluation · 2026-09-05

## Goal and boundaries

Resolve identity, relationship, and attributes as separate questions for brother/sister, pronouns, and forms of address. `Brother` alone does not specify relative age. Birth order must not be inferred from appearance order, occupation, names, or existing translation.

## Conditional examples

The following examples are authored for this proposal and require resolved speakers/referents.

| Evidence | Supported conclusion | Rendering condition |
|---|---|---|
| “Tom is my brother,” said John. | Tom is John's brother; age order unknown | Do not invent older/younger |
| “Tom, my older brother,” said John. | Tom is older relative to John | 哥哥 when literal relationship and references are established |
| “Tom was born two years before John.” plus independent sibling evidence | Sibling relation and Tom older | Both claims must concern the same entities |
| “Tom is like a brother to me.” | Figurative closeness | Not a biological/legal family fact |
| “Brother Thomas” in a monastery | Religious-title candidate | Not somebody's older brother |

Unknown age stays unknown. Use names, a natural general relationship expression, or restructuring as context permits, preserving source information; 兄弟/手足 are not universal replacements.

## Data model

Use a versioned derived relationship store without requiring a graph database. Entities have stable IDs and located name/alias/pronoun mentions with candidate links. Claims carry subject/object/predicate, relational perspective, time/world scope, supporting/refuting source refs and excerpts/ranges, derivation, status, and human decisions. Store `sibling_of` and directed `older_than` separately. Biological, half, adoptive, figurative, and title uses require appropriate distinctions; unknown subtypes stay unknown.

## Evidence workflow

Extract located mentions and candidate claims during prescan. Establish local identity using names/lexical matches/context, then optionally use P06 for distant support and contradiction. Validate quotations, identity, time, and narrative world before bounded inference or agent adjudication. Mark supported/uncertain/conflicted. Attribute dialogue claims to speakers instead of treating them as omniscient facts. Repeated citations of one passage, summaries, and generated translations are not independent factual evidence.

## Time, suspense, and conflicts

Separate whole-book facts from what readers may know at the current position. Whole-book evidence may guide understanding, but translated specificity must respect the current passage's narrative permission; do not reveal later identity twists early. Scope flashbacks, dreams, embedded stories, impersonation, and unreliable narration. Preserve prior claims/provenance when stronger evidence appears and flag dependent passages. Leave unresolved conflicts unresolved.

## Integration and acceptance

Provide only relevant character evidence and permissible specificity to the current batch. Begin with explanatory Review reports before constraining new translation. Completed-text changes remain complete-segment suggestions published only by enabled Autofix. Never force a global glossary mapping from brother to one Chinese term. Test answerable/unanswerable/conflicting stories: multiple siblings, reversed viewpoints, namesakes, aliases, ambiguous pronouns, missing ages, adoption, titles, metaphors, lies, flashbacks, and late reveals. Measure coreference, relation direction, age order, citation support, justified abstention, and unsupported specificity separately; always answering unknown must not pass.

## Sequencing and research

Start with lexical/context tools: entity candidates, explicit older/younger evidence, and read-only reports, estimated 8–12 days. Cross-chapter inference, narrative scope, and publication integration add roughly 10–15 days. Depend on P01/P02/F01/F05; P06 improves recall and P07/P08 consume evidence without circular prerequisites. Extend to uncle/aunt/cousin distinctions later. Literary coreference annotations inform evaluation, but do not solve Chinese age distinctions or suspense preservation. [An Annotated Dataset of Coreference in English Literature](https://aclanthology.org/2020.lrec-1.6/)

## Repository integration points

- [trans_novel/glossary/store.py:34](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/glossary/store.py#L34)
- [trans_novel/agents/analyzer.py:54](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/analyzer.py#L54)
- [trans_novel/review/evidence.py:77](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L77)
- [trans_novel/agents/prompts.py:125](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/prompts.py#L125)
- [trans_novel/pipeline/review_workflow.py:690](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L690)

Engineering estimates assume one contributor and exclude real model/embedding evaluation and long-form human review. Implementation must synchronize config models, built-in/root examples, bilingual docs, and CLI/config tests; default automated tests remain offline.
