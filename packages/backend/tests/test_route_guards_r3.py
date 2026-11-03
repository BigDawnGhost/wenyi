"""Guard additions from worklist round 3 (Y-1/Y-4): exception normalization,
analysis initialization guard, degraded health and import limits."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, Mock

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
