"""Offline glossary search contract tests."""

import pytest
from wenyi_backend.routers import glossary
from wenyi_core.glossary.store import GlossaryTerm


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
