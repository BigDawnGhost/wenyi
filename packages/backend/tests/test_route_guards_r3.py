"""Guard additions from worklist round 3 (Y-1/Y-4): exception normalization,
analysis initialization guard, degraded health and import limits."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import MagicMock, Mock

import httpx2
import pytest
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app


def _client_with(context) -> TestClient:
    return TestClient(create_app(context))


def test_analysis_without_preparation_returns_409(backend_context):
    storage = MagicMock()
    storage.exists.return_value = False
    context = replace(backend_context, storage_for=Mock(return_value=storage))
    response = _client_with(context).get("/projects/pid1/analysis")
    assert response.status_code == 409


def test_analysis_missing_chapter_file_maps_to_404(backend_context):
    storage = MagicMock()
    storage.exists.return_value = True
    storage.load_manifest.return_value = {"chapters": [{"index": 0}]}
    storage.load_chapter.side_effect = FileNotFoundError("chapter not found")
    context = replace(backend_context, storage_for=Mock(return_value=storage))
    response = _client_with(context).get("/projects/pid1/analysis")
    assert response.status_code == 404
    assert response.json()["code"] == "chapter_not_found"


def test_chapter_digest_missing_chapter_file_maps_to_404(backend_context):
    storage = MagicMock()
    storage.exists.return_value = True
    storage.load_chapter.side_effect = FileNotFoundError("chapter not found")
    context = replace(backend_context, storage_for=Mock(return_value=storage))
    response = _client_with(context).put("/projects/pid1/chapter-digests/7", json={"digest": "abc"})
    assert response.status_code == 404
    assert response.json()["code"] == "chapter_not_found"


def test_models_route_maps_invalid_config_to_422(backend_context):
    # A saved project whose languages collapse to the same value makes effective_config
    # raise ValueError; the route must answer 422 invalid_configuration, not a bare 500.
    project = {"id": "pid1", "source_lang": "zh", "target_lang": "zh", "config": {}}
    repository = Mock()
    repository.get_project.return_value = project
    response = _client_with(replace(backend_context, repository=repository)).get(
        "/projects/pid1/models"
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_configuration"


def test_review_mutations_answer_409_when_project_is_unprepared(backend_context):
    # Guards exist on both mutation routes; without them an unprepared project reached
    # the chapter load and answered 500 instead of the documented 409.
    storage = MagicMock()
    storage.exists.return_value = False
    context = replace(backend_context, storage_for=Mock(return_value=storage))
    client = _client_with(context)
    edit = client.put(
        "/projects/pid1/review/0/segments/1",
        json={"expected_target": "a", "target": "b"},
    )
    assert edit.status_code == 409
    complete = client.post("/projects/pid1/review/0/complete")
    assert complete.status_code == 409


def test_glossary_import_accepts_more_than_1000_short_terms(backend_context):
    storage = MagicMock()
    context = replace(backend_context, storage_for=Mock(return_value=storage))
    terms = [{"source": f"t{i}", "target": f"译{i}"} for i in range(2183)]
    response = _client_with(context).post("/projects/pid1/glossary/import", json={"terms": terms})
    assert response.status_code == 200, response.text
    assert response.json() == {"imported": len(terms), "conflicts": 0}
    assert storage.upsert_term.call_count == len(terms)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_glossary_import_limits_utf8_bytes_before_writing(backend_context, streamed, extra_bytes):
    limit = 5 * 1024 * 1024
    note = "译" * (limit // 3 - 100)
    body = json.dumps({"terms": [{"source": "a", "target": "b", "note": note}]}, ensure_ascii=False)
    data = body.encode()
    data += b" " * (limit - len(data) + extra_bytes)
    storage = MagicMock()
    context = replace(backend_context, storage_for=Mock(return_value=storage))
    path = "/projects/pid1/glossary/import"
    headers = {"content-type": "application/json"}

    async def post_stream():
        async def chunks():
            for i in range(0, len(data), 65536):
                yield data[i : i + 65536]
            assert not extra_bytes, "The app must stop reading once the limit is exceeded"

        transport = httpx2.ASGITransport(app=create_app(context))
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, content=chunks(), headers=headers)

    response = (
        asyncio.run(post_stream())
        if streamed
        else _client_with(context).post(path, content=data, headers=headers)
    )
    if extra_bytes:
        assert response.status_code == 413, response.text[:200]
        assert response.json()["code"] == "glossary_import_too_large"
        context.storage_for.assert_not_called()
    else:
        assert response.status_code == 200, response.text[:200]
        assert response.json()["imported"] == 1
        storage.upsert_term.assert_called_once()


def test_health_degrades_without_driver_details(backend_context):
    context = replace(
        backend_context, health=Mock(side_effect=RuntimeError("psycopg: host db.internal:5432"))
    )
    response = _client_with(context).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "unavailable"}
    assert "db.internal" not in response.text


def test_health_ok_reports_ok(backend_context):
    response = _client_with(backend_context).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}
