from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from wenyi_backend.application import create_app


@pytest.mark.parametrize("analysis", [None, {"style_guide": "自然"}])
def test_analysis_reads_only_chapter_digest_projection(backend_context, analysis):
    storage = Mock()
    storage.exists.return_value = True
    storage.load_manifest.return_value = {"chapters": [{"index": 9}, {"index": 2}]}
    storage.load_chapter.side_effect = AssertionError("Analysis must not load chapter bodies")
    storage.read_chapter_digests.return_value = [(9, "First", "摘要"), (2, "", False)]
    storage.load_analysis.return_value = analysis
    context = replace(backend_context, storage_for=Mock(return_value=storage))

    with TestClient(create_app(context)) as client:
        response = client.get("/projects/pid1/analysis")

    assert response.status_code == 200
    assert response.json() == {
        "analysis": analysis or {},
        "chapter_digests": [
            {"index": 9, "title": "First", "digest": "摘要"},
            {"index": 2, "title": "", "digest": ""},
        ],
    }
    context.storage_for.assert_called_once_with("pid1")
    storage.load_manifest.assert_not_called()
    storage.load_chapter.assert_not_called()
    storage.close.assert_called_once()
