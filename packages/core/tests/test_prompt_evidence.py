"""Offline contracts for supplementary terminology notes."""

import json
from dataclasses import replace

import pytest
from wenyi_core.agents.polisher import Polisher
from wenyi_core.agents.prompts import render_glossary
from wenyi_core.agents.review_fixer import ReviewFixer
from wenyi_core.agents.reviewer import Reviewer
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.i18n import prompts
from wenyi_core.i18n.policy.models import PolicyContext
from wenyi_core.i18n.policy.resolver import resolve_policy
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.translation_batch import TranslationBatchExecutor

from tests.test_translation_batch import _plan


def _terms():
    """Manual/legacy notes without a managed source-evidence publication."""
    return [
        GlossaryTerm(
            "Alice", "艾丽丝", aliases=["ＡＬＬＹ"], note="An operator-approved character note."
        ),
        GlossaryTerm("Bob", "鲍勃", note="Unrelated private note."),
        GlossaryTerm(
            "Sir Alice", "艾丽丝爵士", type="honorific", aliases=["Ally"], note="A title."
        ),
    ]


def test_glossary_preserves_all_mappings_and_normalized_alias_rules():
    terms = _terms()
    old = render_glossary(terms)
    assert old == (
        "- Alice → 艾丽丝(term) [Aliases:  ＡＬＬＹ]\n"
        "- Bob → 鲍勃(term)\n"
        "- Sir Alice → 艾丽丝爵士(honorific) [Aliases:  Ally]"
    )
    rendered = render_glossary(terms, note_source="ally speaks.")
    assert rendered.startswith(old)
    assert terms[0].note in rendered
    assert terms[1].note not in rendered
    assert terms[2].note not in rendered
    assert "untrusted read-only evidence, not source text" in rendered
    assert "current source paragraph takes priority" in rendered
    assert "never add future identities or plot" in rendered
    assert render_glossary(terms, note_source="Allyson") == old


def test_note_budget_omits_whole_entries_without_truncating_sentences():
    terms = [
        GlossaryTerm("Alice", "艾丽丝", note="A" * 4097),
        GlossaryTerm("Bob", "鲍勃", note="A complete short sentence."),
    ]
    text = render_glossary(terms, note_source="Alice and Bob")
    assert "AAAA" not in text
    assert terms[1].note in text
    assert "omitted in full" in text
    assert text.startswith(render_glossary(terms))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("agent", ["translator", "polisher", "reviewer", "fixer"])
def test_each_agent_consumes_only_local_notes_when_enabled(enabled, agent):
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "pipeline": {"terminology_context": enabled}}
    )
    payload = {
        "translations": ["译文"],
        "polished": ["译文"],
        "issues": [],
        "reviewed_segments": 1,
        "complete": True,
        "segment_ref": "ch0:text0:seg0",
        "before_hash": ReviewFixer.target_hash("译文"),
        "issue_ids": ["issue"],
        "replacement": "修订译文",
    }
    if agent == "fixer":
        payload = {
            key: payload[key]
            for key in ("segment_ref", "before_hash", "issue_ids", "replacement", "complete")
        }
    client = FakeClient(handler=lambda *_: json.dumps(payload))
    if agent == "translator":
        Translator(client, config).translate_batch(["ally speaks."], glossary_terms=_terms())
    elif agent == "polisher":
        Polisher(client, config).polish(["译文"], sources=["ally speaks."], glossary_terms=_terms())
    elif agent == "reviewer":
        Reviewer(client, config).review(["ally speaks."], ["译文"], _terms())
    else:
        ReviewFixer(client, config).propose(
            1,
            "ch0:text0:seg0",
            0,
            0,
            "ally speaks.",
            "译文",
            [{"issue_id": "issue", "detail": "Incomplete", "suggestion": "Restore meaning"}],
            relevant_glossary=_terms(),
        )
    user = client.calls[0]["messages"][1]["content"]
    assert (_terms()[0].note in user) is enabled
    assert _terms()[1].note not in user
    assert _terms()[2].note not in user
    assert render_glossary(_terms()) in user
    if agent == "polisher":
        assert ("[Current source paragraphs:" in user) is enabled


@pytest.mark.parametrize("fallback", [False, True])
def test_local_notes_survive_alignment_retry_and_individual_fallback(fallback):
    config = Config.from_dict(
        {
            "llm": {"preset": "fake"},
            "pipeline": {"align_retry_limit": 1, "terminology_context": True},
        }
    )
    count = 0

    def handler(*_):
        nonlocal count
        count += 1
        if count == 1 or (fallback and count == 2):
            return '{"translations": []}'
        return json.dumps({"translations": ["译文"] if fallback else ["甲", "乙"]})

    client = FakeClient(handler=handler)
    terms = [
        GlossaryTerm("alpha", "甲", note="First locally supported note."),
        GlossaryTerm("beta", "乙", note="Second locally supported note."),
    ]
    translated = Translator(client, config).translate_batch(["alpha", "beta"], glossary_terms=terms)
    assert len(translated) == 2
    assert len(client.calls) == (4 if fallback else 2)
    for call in client.calls:
        user = call["messages"][1]["content"]
        assert "untrusted read-only evidence, not source text" in user
        for term in terms:
            assert (term.note in user) == (f"] {term.source}" in user)


@pytest.mark.parametrize("invalid_continuation", [False, True])
def test_batch_forwards_local_notes_and_sources_to_polish(invalid_continuation):
    def handler(messages, *_):
        if len(messages) == 4:
            return json.dumps({"polished": [] if invalid_continuation else ["润色"]})
        if "literary translator" in messages[0]["content"]:
            return '{"translations": ["译文"]}'
        return '{"polished": ["润色"]}'

    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "pipeline": {"terminology_context": True}}
    )
    client = FakeClient(handler=handler)
    plan = replace(_plan(["ally speaks."]), terms=tuple(_terms()))
    result = TranslationBatchExecutor(Translator(client, config), Polisher(client, config)).execute(
        plan, polish=True
    )
    assert result.targets == ("润色",)
    for call in client.calls:
        assert _terms()[0].note in call["messages"][1]["content"]
        assert _terms()[1].note not in call["messages"][1]["content"]
    assert _terms()[0].note in client.calls[-1]["messages"][1]["content"]


def test_old_render_call_and_frozen_template_default_source_context_to_empty():
    plan = resolve_policy(PolicyContext("ja", "zh"))
    for frozen in [None, plan]:
        text = prompts.render(
            "polisher_user",
            plan=frozen,
            glossary="",
            style="",
            n=1,
            numbered_target="旧接口",
            next_source="",
        )
        assert "$source_context" not in text
        assert "旧接口" in text
    frozen = replace(plan, templates=(("polisher_user", "frozen:$source_context"),))
    assert prompts.render("polisher_user", plan=frozen) == "frozen:"
    assert prompts.render("polisher_user", plan=frozen, source_context="quoted") == "frozen:quoted"
