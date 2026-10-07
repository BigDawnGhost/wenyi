"""Project creation owns precision choices, independently of application defaults."""

from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection
from wenyi_api import main
from wenyi_api.adapters import create_context
from wenyi_api.global_settings import PostgresSettings
from wenyi_backend.config_documents import config_document
from wenyi_backend.context import use_context
from wenyi_backend.global_settings import GlobalSettings
from wenyi_backend.routers import projects
from wenyi_backend.source_upload import UploadedSource
from wenyi_core.config import Config


@pytest.fixture
def creation_api(monkeypatch, tmp_path):
    defaults = Config.from_dict({"llm": {"preset": "fake"}})
    saved, queued = {}, []

    async def save_source(pid, file, fmt=None):
        path = tmp_path / f"{pid}-{file.filename}"
        path.write_bytes(await file.read())
        return UploadedSource(path, file.filename, projects.input_format(file, fmt), "0" * 64)

    def create_project(name, source_lang, target_lang, strategy, **kwargs):
        pid = kwargs["project_id"]
        saved[pid] = {
            "id": pid,
            "name": name,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "strategy": strategy,
            "config": kwargs["config"],
            **kwargs["source"],
        }

    def set_project_config(pid, config, **kwargs):
        saved[pid]["config"] = config

    def set_project_languages(pid, source, target, **kwargs):
        saved[pid].update(source_lang=source, target_lang=target)

    async def start_job(pid, kind, **kwargs):
        queued.append((pid, kind))
        return {"job_id": "test-job", "project_id": pid, "kind": kind}

    # Substitute only the ports exercised by these rejection/editing contracts.
    storage = SimpleNamespace(lock=lambda **_: nullcontext(), delete_artifact=lambda _: None)
    context = replace(
        create_context(replace(main.settings, data_dir=str(tmp_path), api_token=None)),
        repository=SimpleNamespace(
            create_project=create_project,
            get_project=lambda pid: saved.get(pid),
            set_project_config=set_project_config,
            set_project_languages=set_project_languages,
            set_project_source=lambda pid, **data: saved[pid].update(data),
        ),
        settings_store=SimpleNamespace(
            load=lambda **_: GlobalSettings(defaults),
            guard=lambda **_: nullcontext(None),
        ),
        storage_for=lambda pid: storage,
        project_dir=lambda pid: str(tmp_path / pid),
    )
    monkeypatch.setattr(projects, "save_source", save_source)
    monkeypatch.setattr(projects, "start_job", start_job)
    client = TestClient(main.create_app(context=context))
    with use_context(context):
        yield client, defaults, saved, queued, tmp_path
    client.close()


def test_creation_does_not_mutate_application_defaults(creation_api):
    client, defaults, saved, _, _ = creation_api
    defaults.pipeline.polish = False
    response = client.post(
        "/projects",
        data={"project": '{"name":"Book","translation_mode":"best_of_three"}'},
        files={"file": ("book.txt", b"A fictional traveler crossed a bridge.")},
    )
    assert response.status_code == 201, response.text
    # Unlike the PG snapshot test, these application defaults disable polishing.
    assert saved[response.json()["id"]]["config"]["pipeline"]["polish"] is True
    assert defaults.pipeline.translation_mode == "standard"
    assert defaults.pipeline.polish is False


def test_creation_defaults_to_standard_even_with_legacy_global_precision(creation_api):
    client, defaults, saved, _, _ = creation_api
    defaults.pipeline.translation_mode = "best_of_three"
    response = client.post(
        "/projects",
        data={"project": '{"name":"Book"}'},
        files={"file": ("book.txt", b"A fictional traveler crossed a bridge.")},
    )
    assert response.status_code == 201, response.text
    pipeline = saved[response.json()["id"]]["config"]["pipeline"]
    assert pipeline["translation_mode"] == "standard"
    assert "precision_concurrency" not in pipeline


@pytest.mark.parametrize(
    "choice,filename",
    [
        ('"translation_mode":"unknown"', "book.txt"),
        ('"precision_concurrency":1', "book.txt"),
        ('"precision_concurrency":0', "book.txt"),
        ('"precision_concurrency":4', "book.txt"),
        ('"translation_mode":"best_of_three"', "video.srt"),
        ('"strategy":{"template":"快速出稿"}', "book.txt"),
    ],
)
def test_invalid_creation_choices_publish_nothing(creation_api, choice, filename):
    client, _, saved, queued, tmp_path = creation_api
    response = client.post(
        "/projects",
        data={"project": '{"name":"Book",' + choice + "}"},
        files={"file": (filename, b"A temporary source.")},
    )
    assert response.status_code == 422, response.text
    assert not saved and not queued
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("method,suffix", [("post", "/validate"), ("put", "")])
def test_srt_project_yaml_cannot_enable_precision(creation_api, method, suffix):
    client, _, saved, _, _ = creation_api
    created = client.post(
        "/projects",
        data={"project": '{"name":"Subtitles"}'},
        files={"file": ("video.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    before = deepcopy(saved[pid])
    response = getattr(client, method)(
        f"/projects/{pid}/config{suffix}",
        json={"yaml": "pipeline: {translation_mode: best_of_three, polish: true}"},
    )
    assert response.status_code == 422, response.text
    assert "Create a new project" in response.text
    assert saved[pid] == before


@pytest.mark.parametrize("method,suffix", [("post", "/validate"), ("put", "")])
def test_project_yaml_cannot_configure_initial_draft_concurrency(creation_api, method, suffix):
    client, _, saved, _, _ = creation_api
    created = client.post(
        "/projects",
        data={"project": '{"name":"Book"}'},
        files={"file": ("book.txt", b"A fictional traveler crossed a bridge.")},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    before = deepcopy(saved[pid])
    response = getattr(client, method)(
        f"/projects/{pid}/config{suffix}", json={"yaml": "pipeline: {precision_concurrency: 1}"}
    )
    assert response.status_code == 422, response.text
    assert "built in" in response.text
    assert saved[pid] == before


@pytest.mark.parametrize("section", ["null", "false", "[]", "text"])
@pytest.mark.parametrize("method,suffix", [("post", "/validate"), ("put", "")])
def test_project_yaml_rejects_malformed_pipeline_sections(creation_api, method, suffix, section):
    client, _, saved, _, _ = creation_api
    created = client.post(
        "/projects",
        data={"project": '{"name":"Book"}'},
        files={"file": ("book.txt", b"A fictional traveler crossed a bridge.")},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    before = deepcopy(saved[pid])
    response = getattr(client, method)(
        f"/projects/{pid}/config{suffix}", json={"yaml": f"pipeline: {section}"}
    )
    assert response.status_code == 422, response.text
    assert "mapping" in response.text
    assert saved[pid] == before


def test_precision_project_cannot_replace_source_with_srt(creation_api):
    client, _, saved, queued, tmp_path = creation_api
    created = client.post(
        "/projects",
        data={"project": '{"name":"Book","translation_mode":"best_of_three"}'},
        files={"file": ("book.txt", b"A fictional traveler crossed a bridge.")},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    before, files = deepcopy(saved[pid]), set(tmp_path.iterdir())
    response = client.post(
        f"/projects/{pid}/upload",
        files={"file": ("video.srt", b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")},
    )
    assert response.status_code == 422, response.text
    assert saved[pid] == before
    assert set(tmp_path.iterdir()) == files
    assert queued == [(pid, "parse")]


def test_postgres_settings_normalize_legacy_global_precision():
    precision = Config.from_dict(
        {
            "llm": {"preset": "fake"},
            "pipeline": {"translation_mode": "best_of_three", "precision_concurrency": 1},
        }
    )
    row = (config_document(precision), "标准翻译", 7)
    connection = SimpleNamespace(execute=lambda _: SimpleNamespace(fetchone=lambda: row))
    defaults = PostgresSettings(None, "").load(connection=cast(Connection[Any], connection))
    assert defaults.revision == 7
    assert defaults.config.pipeline.translation_mode == "standard"
    assert "precision_concurrency" not in defaults.config.pipeline.model_dump()


def test_retired_global_template_is_read_as_standard_without_rewriting():
    document = config_document(Config.from_dict({"llm": {"preset": "fake"}}))
    row = (document, "快速出稿", 8)
    before = deepcopy(row)
    connection = SimpleNamespace(execute=lambda _: SimpleNamespace(fetchone=lambda: row))
    defaults = PostgresSettings(None, "").load(connection=cast(Connection[Any], connection))
    assert defaults.default_template == "标准翻译"
    assert defaults.revision == 8
    assert defaults.config.pipeline.polish is True
    assert row == before


@pytest.mark.parametrize("method,suffix", [("post", "/validate"), ("put", "")])
def test_global_routes_reject_retired_template_before_database_access(creation_api, method, suffix):
    client, _, _, _, _ = creation_api
    response = getattr(client, method)(
        f"/settings{suffix}",
        json={"yaml": "llm: {preset: fake}", "default_template": "快速出稿", "revision": 0},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("method,suffix", [("post", "/validate"), ("put", "")])
def test_precision_mode_is_immutable_but_partial_config_remains_editable(
    creation_api, method, suffix
):
    client, defaults, saved, _, _ = creation_api
    created = client.post(
        "/projects",
        data={"project": '{"name":"Book","translation_mode":"best_of_three"}'},
        files={"file": ("book.txt", b"A fictional traveler crossed a bridge.")},
    )
    pid = created.json()["id"]
    before = deepcopy(saved[pid])
    for yaml in ("pipeline: {translation_mode: standard}", "pipeline: {polish: false}"):
        rejected = getattr(client, method)(f"/projects/{pid}/config{suffix}", json={"yaml": yaml})
        assert rejected.status_code == 422, rejected.text
        assert saved[pid] == before
    accepted = getattr(client, method)(
        f"/projects/{pid}/config{suffix}", json={"yaml": "pipeline: {review: false}"}
    )
    assert accepted.status_code == 200, accepted.text
    pipeline = accepted.json()["effective"]["pipeline"]
    assert pipeline["translation_mode"] == "best_of_three"
    assert pipeline["polish"] is True and pipeline["review"] is False
    defaults.pipeline.polish = False
    restored = client.get(f"/projects/{pid}/config/defaults")
    assert restored.status_code == 200, restored.text
    assert restored.json()["effective"]["pipeline"]["translation_mode"] == "best_of_three"
    assert restored.json()["effective"]["pipeline"]["polish"] is True


def test_start_body_cannot_select_retired_template(creation_api):
    client, _, saved, queued, _ = creation_api
    created = client.post(
        "/projects",
        data={"project": '{"name":"Book"}'},
        files={"file": ("book.txt", b"A fictional traveler crossed a bridge.")},
    )
    pid = created.json()["id"]
    before = deepcopy(saved[pid]), deepcopy(queued)
    rejected = client.post(
        f"/projects/{pid}/translate", json={"strategy": {"template": "快速出稿"}}
    )
    assert rejected.status_code == 422, rejected.text
    assert (saved[pid], queued) == before
