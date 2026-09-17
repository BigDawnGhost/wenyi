"""Shared registration, per-project model choices and frozen execution contracts."""

# ruff: noqa: F811

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from test_project_routes import api, new_project  # noqa: F401
from test_storage_pg_integration import pg_pool  # noqa: F401
from type_helpers import must
from wenyi_api import dal
from wenyi_api.db import get_pool
from wenyi_api.model_registry import rename_model_references
from wenyi_api.workers import tasks


@pytest.fixture(autouse=True)
def reset_settings(api):
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM application_settings")
    yield
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM projects")
        conn.execute("DELETE FROM application_settings")


def payload(response, *, config=None, **kwargs):
    return {
        "yaml": json.dumps(config or response["effective"]),
        "revision": response["revision"],
        "default_template": response["default_template"],
        **kwargs,
    }


def test_shared_registry_persists_and_projects_only_save_selections(api):
    client, _ = api
    global_config = client.get("/settings").json()
    document = global_config["effective"]
    document["llm"]["models"]["extra"] = {"provider": "default", "model": "extra-model"}
    saved = client.put("/settings", json=payload(global_config))
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert "extra" in client.get("/settings").json()["effective"]["llm"]["models"]
    pid = new_project(api)
    config = client.get(f"/projects/{pid}/config").json()
    assert set(config["effective"]["llm"]) == {"tiers", "routes", "budget"}
    assert config["registered_models"]["extra"]["model"] == "extra-model"
    config["effective"]["llm"]["tiers"]["strong"] = "extra"
    config["effective"]["llm"]["routes"] = {"translation.body": {"model": "extra"}}
    assert (
        client.put(
            f"/projects/{pid}/config", json={"yaml": json.dumps(config["effective"])}
        ).status_code
        == 200
    )
    assert set(must(dal.get_project(pid))["config"]["llm"]) == {"tiers", "routes", "budget"}
    other = new_project(api)
    assert (
        client.get(f"/projects/{other}/config").json()["effective"]["llm"]["tiers"]["strong"]
        != "extra"
    )
    # Clearing explicit operation routes must remove the override, not merge it back.
    config["effective"]["llm"]["routes"] = {}
    assert (
        client.put(
            f"/projects/{pid}/config", json={"yaml": json.dumps(config["effective"])}
        ).json()["effective"]["llm"]["routes"]
        == {}
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("providers", {}),
        ("models", {}),
        ("preset", "deepseek"),
        ("quotas", {}),
        ("tiers", {"strong": "unregistered"}),
        ("routes", {"translation.body": {"model": "unregistered"}}),
    ],
)
def test_project_yaml_cannot_register_models_or_select_unknown_ids(api, key, value):
    client, _ = api
    pid = new_project(api)
    for suffix, method in (("/validate", client.post), ("", client.put)):
        response = method(
            f"/projects/{pid}/config{suffix}", json={"yaml": json.dumps({"llm": {key: value}})}
        )
        assert response.status_code == 422, response.text
    assert must(dal.get_project(pid))["config"] == {}


def test_global_changes_preserve_existing_projects_and_queued_snapshots(api):
    client, _ = api
    before = client.get("/settings").json()
    created = client.post(
        "/projects",
        data={"project": json.dumps({"name": "Book", "source_lang": "en"})},
        files={"file": ("book.txt", b"A book begins.")},
    ).json()
    pid = created["id"]
    run = dal.list_jobs(pid)[0]
    original = tasks._build_config_for(pid, run["arq_job_id"])
    document = before["effective"]
    document["pipeline"]["polish"] = False
    model_id = document["llm"]["tiers"]["strong"]
    document["llm"]["models"][model_id]["model"] = "new-model"
    changed = client.put("/settings", json=payload(before, default_template="快速出稿"))
    assert changed.status_code == 200, changed.text
    assert tasks._build_config_for(pid, run["arq_job_id"]).llm == original.llm
    existing = client.get(f"/projects/{pid}/config").json()
    assert existing["effective"]["pipeline"]["polish"] is True
    assert existing["registered_models"][model_id]["model"] == "new-model"
    second = client.post(
        "/projects",
        data={"project": json.dumps({"name": "New book"})},
        files={"file": ("new.txt", b"A different book.")},
    ).json()
    assert second["strategy"]["template"] == "快速出稿"
    assert (
        client.get(f"/projects/{second['id']}/config").json()["effective"]["pipeline"]["polish"]
        is False
    )
    templates = client.get("/strategies/templates").json()
    assert [t["name"] for t in templates if t["recommended"]] == ["快速出稿"]
    assert all(t["steps"]["polish"] is False for t in templates)
    assert client.put("/settings", json=payload(before)).status_code == 409


def test_in_use_models_and_secrets_cannot_be_saved(api):
    client, _ = api
    current = client.get("/settings").json()
    doc = current["effective"]
    doc["llm"]["models"]["extra"] = {"provider": "default", "model": "extra"}
    saved = client.put("/settings", json=payload(current)).json()
    pid = new_project(api)
    client.put(f"/projects/{pid}/config", json={"yaml": "llm: {tiers: {strong: extra}}"})
    del saved["effective"]["llm"]["models"]["extra"]
    response = client.put("/settings", json=payload(saved))
    assert response.status_code == 422 and pid in response.text
    saved["effective"]["llm"]["providers"]["default"]["api_key"] = "not-a-real-key"
    assert client.post("/settings/validate", json=payload(saved)).status_code == 422
    assert (
        "api_key" not in client.get("/settings").json()["effective"]["llm"]["providers"]["default"]
    )


def test_concurrent_global_edits_do_not_overwrite_each_other(api):
    from concurrent.futures import ThreadPoolExecutor

    client, _ = api
    current = client.get("/settings").json()
    body = payload(current)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.put("/settings", json=body), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert client.get("/settings").json()["revision"] == 1


@pytest.mark.parametrize("document", ["[]", "false", "paths: {state_dir: /tmp/other}"])
def test_global_yaml_rejects_invalid_documents_and_server_paths(api, document):
    client, _ = api
    current = client.get("/settings").json()
    response = client.put("/settings", json=payload(current, yaml=document))
    assert response.status_code == 422
    assert client.get("/settings").json()["revision"] == 0


def test_project_save_reuses_the_registry_transaction_connection(api, pg_pool):
    client, _ = api
    pid = new_project(api)
    timeout = pg_pool.timeout
    pg_pool.resize(min_size=1, max_size=2)
    pg_pool.timeout = 0.2
    try:
        response = client.put(f"/projects/{pid}/config", json={"yaml": "pipeline: {polish: false}"})
        assert response.status_code == 200, response.text
    finally:
        pg_pool.timeout = timeout
        pg_pool.resize(min_size=1, max_size=8)
    assert must(dal.get_project(pid))["config"]["pipeline"]["polish"] is False


@pytest.mark.parametrize("reuse_id", [False, True])
def test_renaming_updates_projects_atomically_and_preserves_queued_snapshots(api, reuse_id):
    client, _ = api
    current = client.get("/settings").json()
    pid = new_project(api)
    old = current["effective"]["llm"]["tiers"]["strong"]
    project_config = {
        "llm": {
            "tiers": {"strong": old},
            "routes": {
                "translation.body": {"model": old},
                "translation.title": {
                    "model": current["effective"]["llm"]["tiers"]["cheap"],
                    "fallbacks": [old],
                },
            },
        },
        "pipeline": {"polish": False},
    }
    assert (
        client.put(f"/projects/{pid}/config", json={"yaml": json.dumps(project_config)}).status_code
        == 200
    )
    saved_project = deepcopy(must(dal.get_project(pid))["config"])
    job_id = dal.create_job(
        pid,
        "translation",
        "before-rename",
        run_id="before-rename",
        config_snapshot=current["effective"],
    )
    frozen = deepcopy(must(dal.get_job(job_id))["params"])
    llm = current["effective"]["llm"]
    llm["preset"] = None
    llm["providers"]["renamed_provider"] = llm["providers"].pop("default")
    for model in llm["models"].values():
        model["provider"] = "renamed_provider"
    llm["models"]["renamed_model"] = llm["models"].pop(old)
    current["effective"]["llm"] = rename_model_references(llm, {old: "renamed_model"})
    if reuse_id:
        # Existing selections follow the rename even when a new profile reuses the vacated ID.
        current["effective"]["llm"]["models"][old] = {
            "provider": "renamed_provider",
            "model": "newly-registered-model",
        }
    body = payload(current, model_renames={old: "renamed_model"})
    assert client.post("/settings/validate", json=body).status_code == 200
    assert must(dal.get_project(pid))["config"] == saved_project
    assert client.put("/settings", json=body).status_code == 200
    updated = must(dal.get_project(pid))["config"]
    assert updated["pipeline"] == saved_project["pipeline"]
    assert updated["llm"]["tiers"]["strong"] == "renamed_model"
    assert updated["llm"]["routes"]["translation.body"] == {
        "model": "renamed_model",
        "tier": None,
        "fallbacks": [],
    }
    assert updated["llm"]["routes"]["translation.title"]["fallbacks"] == ["renamed_model"]
    assert must(dal.get_job(job_id))["params"] == frozen
    effective = client.get(f"/projects/{pid}/config").json()
    assert effective["registered_models"]["renamed_model"]["provider"] == "renamed_provider"
    assert client.put("/settings", json=body).status_code == 409


def test_invalid_rename_does_not_change_registry_or_project_references(api):
    client, _ = api
    pid = new_project(api)
    before = client.get("/settings").json()
    response = client.put("/settings", json=payload(before, model_renames={"missing": "other"}))
    assert response.status_code == 422
    assert client.get("/settings").json() == before
    assert must(dal.get_project(pid))["config"] == {}


def test_unused_registrations_can_be_deleted_and_defaults_are_only_a_preview(api):
    client, _ = api
    defaults = client.get("/settings/defaults").json()
    current = deepcopy(defaults)
    llm = current["effective"]["llm"]
    llm["providers"]["unused"] = {"kind": "fake"}
    llm["models"]["unused"] = {"provider": "unused", "model": "unused"}
    current["effective"]["pipeline"]["polish"] = False
    saved = client.put("/settings", json=payload(current)).json()
    assert client.get("/settings/defaults").json() == defaults
    assert client.get("/settings").json() == saved
    # Loading defaults must not save them until the caller explicitly submits them.
    reset = client.put("/settings", json=payload(defaults, revision=saved["revision"]))
    assert reset.status_code == 200, reset.text
    assert reset.json()["effective"] == defaults["effective"]


def test_project_reset_uses_global_defaults_and_preserves_language(api):
    client, _ = api
    pid = new_project(api, source="ja", target="en")
    client.put(f"/projects/{pid}/config", json={"yaml": "pipeline: {polish: false}"})
    before = deepcopy(must(dal.get_project(pid))["config"])
    preview = client.get(f"/projects/{pid}/config/defaults").json()
    assert preview["effective"]["language"] == {"source": "ja", "target": "en"}
    assert preview["effective"]["pipeline"]["polish"] is True
    assert must(dal.get_project(pid))["config"] == before
    saved = client.put(f"/projects/{pid}/config", json={"yaml": preview["yaml"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["effective"] == preview["effective"]
