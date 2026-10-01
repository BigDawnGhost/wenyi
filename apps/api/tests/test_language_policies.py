"""CLI and PostgreSQL share policy artifacts and built-in revision semantics."""

# ruff: noqa: F811

import json

import pytest
from test_project_routes import api, new_project  # noqa: F401
from test_storage_pg_integration import pg_pool, pg_storage, storage  # noqa: F401
from tests.fake_llm import routing_handler
from wenyi_api import dal
from wenyi_api.project_service import storage_for
from wenyi_core.config import Config
from wenyi_core.i18n.resources import read_text
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.language_policies import CHECKPOINT
from wenyi_core.pipeline.orchestrator import Orchestrator


def test_policy_artifacts_resume_and_export_are_backend_neutral(storage, tmp_path, monkeypatch):
    source = tmp_path / "book.txt"
    source.write_text("# Chapter\n\nThe door opened.\n", encoding="utf-8")
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "pipeline": {"book_understanding": False, "polish": False, "review": False},
        }
    )
    client = FakeClient(handler=routing_handler)
    Orchestrator(config, client=client, storage=storage).run(str(source))
    checkpoint = storage.read_artifact(CHECKPOINT)
    assert all(
        storage.read_artifact(reference)["schema_version"] == 1 for reference in checkpoint.values()
    )
    before = storage.load_chapter(0).model_dump()
    config.output.punctuation_normalize = False
    resumed = FakeClient(handler=routing_handler)
    orchestrator = Orchestrator(config, client=resumed, storage=storage)
    orchestrator.run(str(source))
    result = orchestrator.run_assemble(
        str(source), out_format="docx", out_path=str(tmp_path / "book.docx")
    )
    assert result["outputs"] == [str(tmp_path / "book.docx")]
    assert resumed.calls == []
    assert storage.read_artifact(CHECKPOINT) == checkpoint
    assert storage.load_chapter(0).model_dump() == before
    monkeypatch.setattr(
        "wenyi_core.i18n.policy.resolver.read_text",
        lambda path: (
            read_text(path)
            + ("\nFollow source evidence." if path == "tasks/analyzer_system.txt" else "")
        ),
    )
    interrupted = Orchestrator(config, client=resumed, storage=storage)
    with monkeypatch.context() as patch:
        patch.setattr(
            interrupted._runtime.analyzer,
            "analyze",
            lambda sample: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            interrupted.run(str(source))
    assert storage.read_artifact(CHECKPOINT) == checkpoint
    assert storage.load_chapter(0).model_dump() == before
    Orchestrator(config, client=resumed, storage=storage).run(str(source))
    assert [call["operation"] for call in resumed.calls] == ["analysis.style"]
    refreshed = storage.read_artifact(CHECKPOINT)
    assert refreshed is not None
    assert refreshed["analysis"] != checkpoint["analysis"]
    assert refreshed["translation"] == checkpoint["translation"]
    assert storage.load_chapter(0).model_dump() == before


def test_project_configuration_exposes_only_language_direction(api):
    client, _ = api
    pid = new_project(api)
    response = client.get(f"/projects/{pid}/config")
    assert response.status_code == 200
    document = response.json()["effective"]
    assert set(document["language"]) == {"source", "target"}
    assert "language_policies" not in response.json()
    assert "language_policies" not in client.get("/settings").json()
    assert client.get(f"/projects/{pid}/language-policy").status_code == 404
    document["language"]["source"] = "en"
    saved = client.put(f"/projects/{pid}/config", json={"yaml": json.dumps(document)})
    assert saved.status_code == 200, saved.text
    assert saved.json()["effective"]["language"] == document["language"]


def test_initialized_source_is_validated_before_saving(api):
    client, _ = api
    pid = new_project(api)
    storage_for(pid).save_manifest(
        {"source_lang": "en", "target_lang": "zh", "fmt": "text", "chapters": []}
    )
    before = dal.get_project(pid)
    assert before is not None
    document = client.get(f"/projects/{pid}/config").json()["effective"]
    document["language"]["source"] = "ja"
    response = client.put(f"/projects/{pid}/config", json={"yaml": json.dumps(document)})
    assert response.status_code == 422
    assert "Source language conflicts" in response.text
    after = dal.get_project(pid)
    assert after is not None
    assert after["config"] == before["config"]


@pytest.mark.parametrize("field", ["operations", "accepted_policy_fingerprint"])
def test_language_policy_fields_are_rejected_without_saving(api, field):
    client, _ = api
    pid = new_project(api)
    before = dal.get_project(pid)
    assert before is not None
    document = client.get(f"/projects/{pid}/config").json()["effective"]
    document["language"][field] = {}
    for method, path in ((client.post, "config/validate"), (client.put, "config")):
        response = method(f"/projects/{pid}/{path}", json={"yaml": json.dumps(document)})
        assert response.status_code == 422
        assert "unknown language configuration fields" in response.text
    after = dal.get_project(pid)
    assert after is not None
    assert after["config"] == before["config"]
