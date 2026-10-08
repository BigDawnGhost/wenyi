"""Language contracts for generated metadata and the default interface."""

import json

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.agents.analyzer import Analyzer
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm, term_match_sources
from wenyi_core.i18n.languages import supported_languages
from wenyi_core.i18n.prompts import render
from wenyi_core.llm.providers.fake import FakeClient


@pytest.mark.parametrize("source", [*supported_languages(), "auto"])
@pytest.mark.parametrize("target", ["en", "ja"])
@pytest.mark.parametrize("task", ["analyzer_system", "glossary_extractor_system"])
def test_glossary_reading_schema_depends_on_source_not_target(task, source, target):
    prompt = render(task, src=source, tgt=target)
    schema = json.loads(prompt[prompt.index("\n{") :])
    entries = schema.get("characters", []) + schema["terms"]
    assert all(("reading" in entry) == (source == "ja") for entry in entries)
    if source == "ja":
        assert '"reading":' in prompt
        assert "furigana" in prompt
        assert "Do not infer a reading" in prompt
        assert "Leave reading empty when the source provides no reading" in prompt
    else:
        assert '"reading":' not in prompt
        assert "pronunciation" not in prompt
        assert "reading records" not in prompt


@pytest.mark.parametrize("source", ["ja", "en", "ru", "zh", "auto"])
def test_analysis_only_accepts_japanese_source_readings(tmp_path, source):
    data = {
        "characters": [{"source": "綾小路", "target": "Ayanokoji", "reading": "あやのこうじ"}],
        "terms": [{"source": "学校", "target": "School", "reading": "がっこう"}],
    }
    analyzer = Analyzer(
        FakeClient(handler=lambda *_: json.dumps(data)),
        Config.from_dict({"language": {"source": source, "target": "en"}}),
    )
    result = analyzer.analyze("綾小路〘あやのこうじ〙は学校〘がっこう〙に行った。")
    expected = {"綾小路": "あやのこうじ", "学校": "がっこう"} if source == "ja" else {}
    for entry in result["characters"] + result["terms"]:
        assert entry.get("reading", "") == expected.get(entry["source"], "")

    store = GlossaryStore(str(tmp_path / "glossary.db"))
    try:
        # Also guard seeding from an older, unnormalized analysis snapshot.
        assert analyzer.seed_glossary(store, data) == 2
        for term in store.all_terms():
            assert term.reading == expected.get(term.source, "")
    finally:
        store.close()


def test_non_japanese_analysis_does_not_erase_manually_saved_readings(tmp_path):
    analyzer = Analyzer(
        FakeClient(), Config.from_dict({"language": {"source": "en", "target": "ja"}})
    )
    store = GlossaryStore(str(tmp_path / "glossary.db"))
    try:
        store.upsert_term(
            GlossaryTerm(source="Alice", target="アリス", reading="Manual pronunciation")
        )
        analyzer.seed_glossary(
            store,
            {
                "characters": [
                    {"source": "Alice", "target": "アリス", "reading": "Unexpected model reading"}
                ]
            },
        )
        term = store.get_term("Alice")
        assert term is not None
        assert term.reading == "Manual pronunciation"
    finally:
        store.close()


@pytest.mark.parametrize(
    "target,name", [("en", "English"), ("ja", "Japanese"), ("zh", "Simplified Chinese")]
)
@pytest.mark.parametrize("task", ["analyzer_system", "glossary_extractor_system"])
def test_metadata_language_is_explicit(task, target, name):
    prompt = render(task, src="ru", tgt=target)
    assert f"Write all human-readable metadata in {name}" in prompt
    assert "including every note" in prompt
    assert "Preserve source and aliases" in prompt
    assert "male|female|unknown" in prompt


def test_metadata_round_trip_preserves_source_aliases_and_notes(tmp_path):
    store = GlossaryStore(str(tmp_path / "glossary.db"))
    try:
        store.conn.execute(
            "INSERT INTO glossary (source,target,type,gender,aliases,note) VALUES (?,?,?,?,?,?)",
            ("Вадик", "Vadik", "appellation", "male", '["Вадим"]', "Existing evidence"),
        )
        store.conn.commit()
        term = store.get_term("Вадик")
        assert term is not None
        assert term.type == "appellation"
        assert term.gender == "male"
        assert term_match_sources(term) == ["Вадик"]
        assert term.aliases == ["Вадим"]
        assert term.note == "Existing evidence"
        assert store.conn.execute("SELECT type FROM glossary").fetchone()[0] == "appellation"
        store.upsert_term(
            GlossaryTerm(source="Люда", target="Lyuda", type="person", gender="female")
        )
        assert tuple(
            store.conn.execute("SELECT type,gender FROM glossary WHERE source='Люда'").fetchone()
        ) == ("person", "female")
    finally:
        store.close()


def test_analysis_preserves_style_bullets_and_normalizes_character_metadata():
    data = {
        "style_guide": ["Keep the sparse dialogue.", "Preserve ambiguity."],
        "characters": [{"source": "Вадим", "target": "Vadim", "gender": "male"}],
        "terms": [{"source": "Москва", "target": "Moscow", "type": "place"}],
    }
    analyzer = Analyzer(
        FakeClient(handler=lambda *_: json.dumps(data)),
        Config.from_dict({"language": {"source": "ru", "target": "en"}}),
    )
    result = analyzer.analyze("A short synthetic sample.")
    assert "Keep the sparse dialogue." in result["style_guide"]
    assert "Preserve ambiguity." in result["style_guide"]
    assert result["characters"][0]["source"] == "Вадим"
    assert result["characters"][0]["target"] == "Vadim"
    assert result["characters"][0]["gender"] == "male"
    assert result["terms"][0]["type"] == "place"


def test_cli_and_generated_configuration_use_english(tmp_path):
    path = tmp_path / "config.yaml"
    result = CliRunner().invoke(app, ["--config", str(path), "--help"])
    assert result.exit_code == 0
    assert "Multilingual translation" in result.output
    assert not any("\u3400" <= c <= "\u9fff" for c in result.output)
    comments = [line.partition("#")[2] for line in path.read_text().splitlines() if "#" in line]
    assert not any("\u3400" <= c <= "\u9fff" for c in "\n".join(comments))
