"""Tests for per-request glossary token budgeting and context-window preflight checks."""

from __future__ import annotations

import json
import os

import pytest
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.glossary.prompt_scope import estimate_term_tokens, fit_terms_to_budget, prompt_terms
from wenyi_core.llm.configuration import effective_context_window
from wenyi_core.llm.preflight import fit_prompt_to_context_window
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler
from tests.pipeline_fixtures import fake_pipeline_config, require_file_storage
from tests.sample_data import write_sample_txt


def test_prompt_terms_all_mode_is_append_stable_and_keeps_matching_tail():
    early = [GlossaryTerm(source=f"Early{i}", target=f"早{i}") for i in range(5)]
    later = [GlossaryTerm(source=f"Late{i}", target=f"晚{i}") for i in range(10)]
    budget = sum(estimate_term_tokens(t) for t in early[:3])

    first = prompt_terms([*early, *later[:5]], "Late4 appears here.", budget=budget, mode="all")
    second = prompt_terms([*early, *later], "Late4 appears here.", budget=budget, mode="all")

    # Leading prefix is identical as more terms are appended, and matching tail term is kept.
    assert [t.source for t in first] == ["Early0", "Early1", "Early2", "Late4"]
    assert [t.source for t in second] == ["Early0", "Early1", "Early2", "Late4"]


def test_prompt_terms_relevant_mode_respects_budget_cap():
    terms = [GlossaryTerm(source=f"Hero{i}", target=f"英雄{i}") for i in range(10)]
    text = " ".join(t.source for t in terms)
    budget = sum(estimate_term_tokens(t) for t in terms[:3])
    selected = prompt_terms(terms, text, budget=budget, mode="relevant")
    assert [t.source for t in selected] == ["Hero0", "Hero1", "Hero2"]
    # Zero budget in relevant mode keeps all matching terms.
    assert len(prompt_terms(terms, text, budget=0, mode="relevant")) == 10


def test_fit_terms_to_budget_prioritizes_matching_terms_in_insertion_order():
    terms = [
        GlossaryTerm(source="Alpha", target="阿尔法"),
        GlossaryTerm(source="Beta", target="贝塔"),
        GlossaryTerm(source="Gamma", target="伽马"),
        GlossaryTerm(source="Delta", target="德尔塔"),
    ]
    two_term_budget = estimate_term_tokens(terms[0]) + estimate_term_tokens(terms[3])
    fitted = fit_terms_to_budget(terms, "Delta arrived.", max_tokens=two_term_budget)
    assert [t.source for t in fitted] == ["Alpha", "Delta"]


def test_effective_context_window_defaults_and_explicit_override():
    assert effective_context_window("deepseek", "deepseek-chat", None) == 128_000
    assert effective_context_window("gemini", "gemini-2.5-pro", None) == 1_000_000
    assert effective_context_window("openai", "gpt-4o", None) == 128_000
    assert effective_context_window("openai", "gpt-5", None) == 200_000
    assert effective_context_window("fake", "fake", None) is None
    assert effective_context_window("deepseek", "deepseek-chat", 32_000) == 32_000


def test_preflight_shrinks_glossary_to_fit_small_context_window():
    config = Config.from_dict(
        {
            "llm": {
                "preset": "fake",
                "models": {
                    "small": {
                        "provider": "default",
                        "model": "fake",
                        "context_window": 1400,
                        "max_output_tokens": 200,
                    }
                },
                "tiers": {"strong": "small", "cheap": "small", "fast": "small"},
            }
        }
    )
    terms = [GlossaryTerm(source=f"Term{i:03d}", target=f"术语{i:03d}") for i in range(200)]
    client = FakeClient(handler=lambda *args: json.dumps({"translations": ["译文"]}))
    translator = Translator(client, config)
    out = translator.translate_batch(["Term150 appeared."], glossary_terms=terms)
    assert out == ["译文"]
    sent_prompt = client.calls[0]["messages"][1]["content"]
    # The matched term survived shrinking, while the prompt stayed bounded.
    assert "Term150" in sent_prompt
    assert "Term199" not in sent_prompt


def test_preflight_fails_with_actionable_error_when_even_empty_glossary_overflows():
    config = Config.from_dict(
        {
            "llm": {
                "preset": "fake",
                "models": {
                    "tiny": {
                        "provider": "default",
                        "model": "fake",
                        "context_window": 50,
                        "max_output_tokens": 20,
                    }
                },
                "tiers": {"strong": "tiny", "cheap": "tiny", "fast": "tiny"},
            }
        }
    )
    with pytest.raises(ValueError, match="glossary_prompt_tokens.*batch_glossary"):
        fit_prompt_to_context_window(
            config=config,
            operation="translation.body",
            terms=[],
            match_text="x",
            render_messages=lambda _terms: ("system " * 40, "user " * 40),
        )


def test_changing_glossary_budget_on_resume_does_not_retranslate_completed_batches(tmp_path):
    txt = os.path.join(tmp_path, "novel.txt")
    write_sample_txt(txt)
    config = fake_pipeline_config(os.path.join(tmp_path, "state"))
    config.pipeline.review = False
    config.pipeline.polish = False
    config.pipeline.book_understanding = False
    config.pipeline.glossary_prompt_tokens = 4000

    store = require_file_storage(
        Orchestrator(config, client=FakeClient(handler=routing_handler)).run(txt)
    )
    assert isinstance(store, FileStorage)

    # Change glossary budget and resume: zero translation calls should occur.
    config.pipeline.glossary_prompt_tokens = 250
    resume_client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=resume_client).run(txt)
    assert not [c for c in resume_client.calls if c["operation"] == "translation.body"]
