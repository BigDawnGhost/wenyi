"""Offline glossary search contract tests."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from wenyi_backend.routers import glossary
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.mark.parametrize(
    "query", ["  rEaDiNg  ", "  nOtE  ", "  aLiAs  ", "  sOuRcE  ", "  tArGeT  ", "   "]
)
@pytest.mark.parametrize("term_type,expected", [(None, 2), ("person", 1), ("place", 0)])
def test_list_terms_searches_text_fields_and_preserves_type_filter(
    backend_context, query, term_type, expected
):
    backend_context.repository.get_project.return_value = {"id": "project-1", "fmt": "text"}
    terms = [
        GlossaryTerm(
            source=f"Source {kind}",
            target=f"Target {kind}",
            reading="Reading",
            note="Note",
            aliases=["Alias"],
            type=kind,
        )
        for kind in ("person", "term")
    ]
    backend_context.storage_for.return_value.all_terms.return_value = terms

    result = glossary.list_terms("project-1", q=query, type=term_type)

    assert result == [vars(term) for term in terms[:expected]]


def test_term_pages_sort_by_source_without_reordering_context(backend_context, tmp_path):
    storage = SqliteStorage(str(tmp_path / "project"))
    sources = ["𐀀", "中", "a", "Z", "Å", "\ue000"]
    for source in sources:
        storage.upsert_term(GlossaryTerm(source=source, target="Translation"))
    backend_context.repository.get_project.return_value = {"id": "project-1", "fmt": "text"}
    backend_context.read_storage_for.return_value = storage
    app = FastAPI()
    app.include_router(glossary.router)
    with TestClient(app) as client:
        result = client.get("/projects/project-1/glossary/terms/page?offset=2&limit=2")
        assert result.status_code == 200, result.text
        assert result.json()["total"] == len(sources)
        assert [term["source"] for term in result.json()["items"]] == sorted(sources)[2:4]
        assert client.get("/projects/project-1/glossary/terms/page?offset=100").json() == {
            "items": [],
            "total": len(sources),
        }
        for query in ("offset=-1", "limit=0", "limit=201"):
            assert client.get(f"/projects/project-1/glossary/terms/page?{query}").status_code == 422
    assert [term.source for term in storage.all_terms()] == sources
    storage.close()


def test_term_lookup_uses_full_glossary_not_current_filter(backend_context, tmp_path):
    storage = SqliteStorage(str(tmp_path / "project"))
    storage.upsert_term(GlossaryTerm(source="Hidden", target="Existing", type="person"))
    backend_context.repository.get_project.return_value = {"id": "project-1", "fmt": "text"}
    backend_context.read_storage_for.return_value = storage
    app = FastAPI()
    app.include_router(glossary.router)
    with TestClient(app) as client:
        response = client.post(
            "/projects/project-1/glossary/terms/lookup", json={"sources": ["Hidden", "Missing"]}
        )
        assert response.status_code == 200, response.text
        assert [term["source"] for term in response.json()] == ["Hidden"]
        assert (
            client.post(
                "/projects/project-1/glossary/terms/lookup", json={"sources": ["x"] * 501}
            ).status_code
            == 422
        )
    storage.close()
