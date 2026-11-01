"""Offline checks of the actual Anki package, not just the export response."""

import io
import json
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace

import pytest
from wenyi_core.glossary import anki
from wenyi_core.glossary.store import GlossaryTerm

TYPES = (
    "person",
    "place",
    "organization",
    "term",
    "technique",
    "item",
    "setting",
    "appellation",
    "honorific",
    "speech",
    "fixed_expression",
    "文化::語 彙",
    "文化:語 彙",
)


def _export(terms, **options):
    return anki.export_anki_glossary(
        terms,
        **{
            "project_id": "project-1",
            "title": "Example book",
            "source_lang": "ja",
            "target_lang": "zh",
            **options,
        },
    )


def _read_package(content, tmp_path):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert set(archive.namelist()) == {"collection.anki2", "media"}
        assert json.loads(archive.read("media")) == {}
        database = tmp_path / "export.anki2"
        database.write_bytes(archive.read("collection.anki2"))
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        collection = connection.execute("SELECT * FROM col").fetchone()
        assert collection["ver"] == 11
        assert connection.execute("SELECT COUNT(*) FROM revlog").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM graves").fetchone()[0] == 0
        return {
            "models": json.loads(collection["models"]),
            "decks": json.loads(collection["decks"]),
            "conf": json.loads(collection["conf"]),
            "dconf": json.loads(collection["dconf"]),
            "notes": [dict(row) for row in connection.execute("SELECT * FROM notes")],
            "cards": [dict(row) for row in connection.execute("SELECT * FROM cards")],
        }


def test_all_types_and_custom_categories_export_one_card_per_term(tmp_path):
    terms = [
        GlossaryTerm(source=f"原文 {index:03}", target="译文", type=kind, status="conflict")
        for index, kind in enumerate(TYPES)
    ]
    package = _read_package(_export(reversed(terms)), tmp_path)
    assert len(package["notes"]) == len(package["cards"]) == len(terms)
    assert len(package["models"]) == 1
    assert len({card["did"] for card in package["cards"]}) == len(TYPES)
    assert package["conf"]["nextPos"] == len(terms) + 1
    assert package["conf"]["curModel"] in package["models"]
    for term, note, card in zip(terms, package["notes"], package["cards"]):
        fields = note["flds"].split("\x1f")
        assert fields[0] == term.source
        assert fields[5] == term.type
        assert fields[9] == "conflict"
        assert card["nid"] == note["id"]
        assert card["ord"] == 0
        assert card["type"] == card["queue"] == 0
        assert card["reps"] == 0
        assert card["ivl"] == card["lapses"] == card["odid"] == 0
        deck = package["decks"][str(card["did"])]
        assert str(deck["conf"]) in package["dconf"]
        assert str(note["mid"]) in package["models"]
        name = deck["name"]
        assert name.startswith("Wenyi::Example book [project-1]::")
        assert name.count("::") == 2
        tags = note["tags"].split()
        assert len(tags) == 5
        assert "wenyi::project::project-1" in tags
        assert "wenyi::source::ja" in tags
        assert "wenyi::target::zh" in tags
        assert any(tag.startswith("wenyi::type::") for tag in tags)


def test_fields_are_plain_text_and_reading_is_only_on_the_answer(tmp_path):
    term = GlossaryTerm(
        source="<script>原文</script> & [sound:untrusted.mp3]\x1f",
        target="<b>译文</b>",
        reading="げんぶん",
        note='A "quoted" note\r\nSecond line',
        aliases=["別名", "<img src=x onerror=alert(1)>"],
    )
    package = _read_package(_export([term]), tmp_path)
    fields = package["notes"][0]["flds"].split("\x1f")
    assert fields == [
        "&lt;script&gt;原文&lt;/script&gt; &amp; &#91;sound:untrusted.mp3]&#31;",
        "&lt;b&gt;译文&lt;/b&gt;",
        "げんぶん",
        "A &quot;quoted&quot; note<br>Second line",
        "別名<br>&lt;img src=x onerror=alert(1)&gt;",
        "term",
        "Example book",
        "ja",
        "zh",
        "ok",
    ]
    model = next(iter(package["models"].values()))
    assert len(model["tmpls"]) == 1
    template = model["tmpls"][0]
    assert "{{Source}}" in template["qfmt"]
    for field in ("Reading", "Target", "Note"):
        assert "{{" + field + "}}" not in template["qfmt"]
        assert "{{" + field + "}}" in template["afmt"]
    assert model["req"] == [[0, "all", [0]]]


@pytest.mark.parametrize(
    "source,sort_field,checksum",
    [
        ("a\r\nb", "ab", 3659751758),
        ("<b>x</b>", "<b>x</b>", 3892232842),
        ("[sound:x]", "[sound:x]", 1419279970),
        ("a\x1fb", "a\x1fb", 1753047449),
    ],
)
def test_sort_field_and_checksum_match_anki_plain_text_rules(
    tmp_path, source, sort_field, checksum
):
    package = _read_package(_export([GlossaryTerm(source=source, target="Translation")]), tmp_path)
    note = package["notes"][0]
    assert note["sfld"] == sort_field
    assert note["csum"] == checksum


def test_reexport_keeps_note_model_and_deck_identities(tmp_path):
    term = GlossaryTerm(source="先輩", target="前辈", type="appellation")
    original = _read_package(_export([term]), tmp_path)
    updated = _read_package(
        _export(
            [
                GlossaryTerm(source="後輩", target="后辈"),
                replace(term, target="学长", note="Updated"),
            ],
            title="Renamed book",
        ),
        tmp_path,
    )
    note = next(note for note in updated["notes"] if note["flds"].startswith("先輩\x1f"))
    card = next(card for card in updated["cards"] if card["nid"] == note["id"])
    assert note["guid"] == original["notes"][0]["guid"]
    assert note["mid"] == original["notes"][0]["mid"]
    assert note["flds"].split("\x1f")[1] == "学长"
    assert card["did"] == original["cards"][0]["did"]
    reclassified = _read_package(_export([replace(term, type="term")]), tmp_path)
    assert reclassified["notes"][0]["guid"] == note["guid"]
    assert reclassified["cards"][0]["did"] != card["did"]
    other_project = _read_package(_export([term], project_id="project-2"), tmp_path)
    assert other_project["notes"][0]["guid"] != note["guid"]
    assert other_project["cards"][0]["did"] != card["did"]
    renamed_source = _read_package(_export([replace(term, source="先輩さん")]), tmp_path)
    assert renamed_source["notes"][0]["guid"] != note["guid"]


def test_empty_glossary_is_a_valid_empty_package(tmp_path):
    package = _read_package(_export([]), tmp_path)
    assert package["notes"] == package["cards"] == []
    assert any(
        deck["name"] == "Wenyi::Example book [project-1]" for deck in package["decks"].values()
    )


@pytest.mark.parametrize("failure", [None, "database", "archive"])
def test_temporary_glossary_database_is_removed_even_on_failure(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(anki.tempfile, "tempdir", str(tmp_path))
    if failure:

        def write_failure(*args):
            assert list(tmp_path.glob("wenyi-anki-*/collection.anki2"))
            raise OSError("Fixture write failure")

        if failure == "database":
            monkeypatch.setattr(anki, "_model", write_failure)
        else:
            monkeypatch.setattr(anki.zipfile.ZipFile, "write", write_failure)
        with pytest.raises(OSError, match="Fixture write failure"):
            _export([GlossaryTerm(source="Private source", target="Translation")])
    else:
        _export([GlossaryTerm(source="Private source", target="Translation")])
    assert list(tmp_path.iterdir()) == []
