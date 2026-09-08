"""Pipeline facade, shared runtime and domain services.
Dependencies flow downward; no lower module may import orchestrator.py. The orchestrator
owns routing, stage order, lock scopes and exception propagation. Runtime owns
Config/LLMClient/agents, language restoration, events, usage checkpoints and source validation.
Preparation handles lookup, parsing, initialization, analysis, glossary seeds and prescans.
Annotation handles source context, continuations and alignment. Translation handles batches,
polishing, glossary updates and titles. Review owns evidence, arbitration, shadow revisions
and blind rechecks. ReviewAutofix separately publishes idempotent complete-paragraph
revisions after verification. Finalization owns reports and live/snapshot exports.
Top-level i18n contains pure language helpers; runstore persists state; context manages recent
translations. Review evidence lives in
trans_novel.review, while the independent subtitle workflow lives in trans_novel.srt.
"""
