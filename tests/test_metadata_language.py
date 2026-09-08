"""Language contracts for generated metadata and the default interface."""

import json

import pytest
from typer.testing import CliRunner

from trans_novel.agents.analyzer import Analyzer
from trans_novel.cli import app
from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm, term_match_sources
from trans_novel.i18n.prompts import render
from trans_novel.llm.providers.fake import FakeClient


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
