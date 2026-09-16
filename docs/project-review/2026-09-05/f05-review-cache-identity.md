# F05 · Review cache identity omits models and glossary evidence

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f05-review-cache-identity.md)

Priority：**P2** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

Reproduced: changing the cheap-tier model actually used by the initial Reviewer, or glossary gender/aliases, can return the previous result with zero calls. Users may believe their new model or evidence was evaluated. Incomplete-run compatibility uses the same insufficient identities. Evidence-loop/Fixer models must also participate in identity.

## Evidence

The snapshot records selected review switches, omitting provider, resolved models/options, prompt version, and analysis/digest inputs. Glossary hashing includes only source/target/type, although evidence exposes gender, aliases, notes, readings, and other fields. Metrics already has a broader configuration identity, but Review does not use it.

## Reproduction

Probe F05 completes a single-segment Review and checks that it used the cheap tier and received glossary gender in its prompt. Separate model-only and glossary-only changes then both yield identical fingerprints, the same run directory, and no new calls.

## Proposed change

Version a semantic Review identity covering resolved models and nonsecret options, semantic chunk/scope settings, prompts, source/target, analysis/chapter digests, and visible glossary evidence. Separate scheduling-only settings where appropriate. Add explicit fresh-review control and explain reuse.

## Acceptance and delivery

Individually vary each semantic input and reject stale completed results/checkpoints; reject legacy caches missing the new identity. Never persist secrets. Estimate 2–4 days; some old caches will require paid recomputation. Update both documentation languages: current README/configuration claims of always restarting conflict with existing skip/resume behavior.

## Source and test locations

- [trans_novel/pipeline/review_workflow.py:271](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L271)
- [trans_novel/pipeline/review_workflow.py:292](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L292)
- [trans_novel/pipeline/review_workflow.py:690](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L690)
- [trans_novel/review/evidence.py:265](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/evidence.py#L265)
- [trans_novel/pipeline/metrics.py:118](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/metrics.py#L118)
- [tests/test_orchestrator.py:1367](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_orchestrator.py#L1367)

Reproduce using the shared script in the index; inspect `F05` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
