"""Display pagination must not alter prompt insertion order or Unicode search."""

import pytest
from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.fixture(params=["glossary", "file", "sqlite"])
def glossary_storage(request, tmp_path):
    if request.param == "glossary":
        storage = GlossaryStore(str(tmp_path / "glossary.db"))
    elif request.param == "file":
        storage = FileStorage(str(tmp_path / "file"))
    else:
        storage = SqliteStorage(str(tmp_path / "sqlite"))
    yield storage
    storage.close()


def test_pages_preserve_prompt_order_and_match_unicode_codepoints(glossary_storage):
    sources = ["𐀀", "z", "中", "A", "\ue000", "ä"]
    for source in sources:
        glossary_storage.upsert_term(GlossaryTerm(source=source, target=source))
    items, total = glossary_storage.list_terms_page(offset=1, limit=3)
    assert total == len(sources)
    assert [term.source for term in items] == sorted(sources)[1:4]
    assert glossary_storage.list_terms_page(offset=100, limit=3) == ([], total)
    assert [term.source for term in glossary_storage.all_terms()] == sources
    assert {term.source for term in glossary_storage.get_terms(["z", "A", "absent"])} == {"z", "A"}
    assert glossary_storage.get_terms([]) == []


@pytest.mark.parametrize("query", ["STRASSE", "reading", "alias", "note", "%_", "   "])
def test_search_applies_before_pagination(glossary_storage, query):
    for source in ["Z", "A", "B"]:
        glossary_storage.upsert_term(
            GlossaryTerm(
                source=source,
                target="Straße %_",
                reading="Reading",
                aliases=["Alias"],
                note="Note",
                type="person",
            )
        )
    glossary_storage.upsert_term(GlossaryTerm(source="Other", target="Straße %_"))
    items, total = glossary_storage.list_terms_page(q=query, term_type="person", offset=1, limit=1)
    assert total == 3
    assert [term.source for term in items] == ["B"]
    assert glossary_storage.list_terms_page(q="does not exist") == ([], 0)
