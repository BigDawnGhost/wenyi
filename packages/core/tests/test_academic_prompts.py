"""Offline resource contracts for the academic-book genre; the default remains literary."""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

import pytest
from wenyi_core.agents.analyzer import Analyzer
from wenyi_core.config import Config
from wenyi_core.i18n.genres import compose
from wenyi_core.i18n.resources import read_json, read_text
from wenyi_core.llm.providers.fake import FakeClient

DATA = Path(__file__).resolve().parents[1] / "wenyi_core/i18n/data"
RESOURCES = tuple(
    sorted(
        path.relative_to(DATA).as_posix()
        for directory in ("tasks", "shared", "languages", "pairs")
        for path in (DATA / directory).iterdir()
        if path.suffix in {".txt", ".json"}
    )
)


@pytest.mark.parametrize("path", RESOURCES)
def test_academic_resources_compose_without_missing_or_fiction_specific_slots(path):
    text = compose(read_text(path), genre="academic")
    assert "${genre_" not in text
    assert not re.search(r"\b(novels?|fiction|fictional|literary|plot|foreshadowing)\b", text)
    if path.endswith(".json"):
        assert isinstance(json.loads(text), dict)
    variables = {
        match.group("named") or match.group("braced")
        for match in Template.pattern.finditer(text)
        if match.group("named") or match.group("braced")
    }
    Template(text).substitute({key: "fixture" for key in variables})


def test_academic_explicitly_overrides_all_literary_slots():
    literary = read_json("genres/literary/fragments.json")
    academic = read_json("genres/academic/fragments.json")
    assert academic.keys() == literary.keys()
    for key in academic:
        assert compose("${" + key + "}", genre="academic") == academic[key]


def test_academic_analysis_uses_supported_style_fields_instead_of_character_profiles():
    text = compose(read_text("tasks/analyzer_system.txt"), genre="academic")
    text = Template(text).substitute(src_label="English", tgt_label="Simplified Chinese")
    schema = json.loads(text[text.index("\n{") :])
    assert set(schema) == {
        "genre",
        "tone",
        "style_guide",
        "narration",
        "pacing",
        "register",
        "rhetoric",
    }
    assert all(isinstance(value, str) for value in schema.values())

    analyzer = Analyzer(FakeClient(), Config())
    brief = analyzer.style_brief(schema)
    for key in ("narration", "pacing", "rhetoric"):
        assert schema[key] in brief
    assert "Characters:" not in brief


def test_academic_translation_protects_arguments_attribution_and_reference_identities():
    text = compose(read_text("tasks/translator_system.txt"), genre="academic")
    for requirement in (
        "logical scope, modality, quantifiers, negation and degree of certainty",
        "Distinguish the author's claims from quotations",
        "citation keys, bibliographic identifiers and cross-reference labels",
        "Do not add explanations, invent or repair citations",
        "retain source ambiguities and inconsistencies",
    ):
        assert requirement in text


def test_academic_prescans_do_not_require_an_experimental_paper_structure():
    digest = compose(read_text("tasks/chapter_digest_system.txt"), genre="academic")
    synopsis = compose(read_text("tasks/book_synopsis_system.txt"), genre="academic")
    assert "Mention methods and findings only when present" in digest
    assert "Describe methods and findings only when present" in synopsis
    assert "theoretical, historical or interpretive works" in synopsis
    assert "or turn tentative claims into established facts" in synopsis


def test_academic_polishing_targets_clarity_not_literary_rewriting():
    for task in ("polisher_system", "polisher_continue_user"):
        text = compose(read_text(f"tasks/{task}.txt"), genre="academic")
        assert "Improve fluency and academic clarity" in text
        assert "without changing meaning" in text


def test_academic_terminology_does_not_expand_experimental_person_only_discovery():
    extractor = compose(read_text("tasks/glossary_extractor_system.txt"), genre="academic")
    discovery = compose(read_text("tasks/terminology_discover_system.txt"), genre="academic")
    assert (
        "discipline-specific concepts, theories, methods, measures and classifications" in extractor
    )
    assert "Find only people and their source identities" in discovery
    assert 'with type "person"' in discovery
    assert "Do not propose places, organizations, technical terms" in discovery
