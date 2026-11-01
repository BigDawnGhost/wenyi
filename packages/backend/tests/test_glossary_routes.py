"""Offline glossary search contract tests."""

import io
import zipfile
from unittest.mock import Mock

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


def test_anki_export_uses_full_glossary_and_closes_storage(backend_context, monkeypatch):
    backend_context.repository.get_project.return_value = {
        "id": "project-1",
        "name": "Project name",
        "book_title": "Book title",
        "fmt": "txt",
        "source_lang": "ja",
        "target_lang": "zh",
    }
    terms = [
        GlossaryTerm(
            source=f"原文 {index:03}", target="译文", type="person" if index % 2 else "term"
        )
        for index in range(135)
    ]
    storage = backend_context.storage_for.return_value
    storage.all_terms.return_value = terms
    export = Mock(wraps=glossary.export_anki_glossary)
    monkeypatch.setattr(glossary, "export_anki_glossary", export)
    app = FastAPI()
    app.include_router(glossary.router)
    with TestClient(app) as client:
        response = client.get("/projects/project-1/glossary/export?format=apkg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == "attachment; filename=glossary-project-1.apkg"
    assert int(response.headers["content-length"]) == len(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"collection.anki2", "media"}
    export.assert_called_once_with(
        terms, project_id="project-1", title="Book title", source_lang="ja", target_lang="zh"
    )
    storage.all_terms.assert_called_once_with()
    storage.close.assert_called_once_with()
    backend_context.enqueue.assert_not_called()
    storage.upsert_term.assert_not_called()
    storage.log_event.assert_not_called()


def test_anki_export_rejects_missing_projects_and_subtitles(backend_context):
    app = FastAPI()
    app.include_router(glossary.router)
    with TestClient(app) as client:
        backend_context.repository.get_project.return_value = None
        assert client.get("/projects/missing/glossary/export?format=apkg").status_code == 404
        backend_context.repository.get_project.return_value = {"id": "subtitles", "fmt": "srt"}
        assert client.get("/projects/subtitles/glossary/export?format=apkg").status_code == 422
    backend_context.storage_for.assert_not_called()
