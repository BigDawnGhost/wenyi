"""Settings reconstruction never rewrites project state, metadata or frozen jobs."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from wenyi_desktop.local_backend import LocalBackend


@pytest.fixture
def saved_catalog(tmp_path):
    backend = LocalBackend(tmp_path / "workspace")
    settings = {
        "config": {"llm": {"preset": "fake"}, "honorific": {"strategy": "drop"}},
        "template": "标准翻译",
        "revision": 5,
        "library_metadata": ["preserve-this"],
    }
    project = {
        "id": "synthetic",
        "status": "done",
        "source_lang": "en",
        "target_lang": "zh",
        "config": {
            "pipeline": {
                "translation_mode": "best_of_three",
                "precision_concurrency": 1,
                "review": False,
                "prompt_genre": "academic",
            },
            "output": {"bilingual": True},
        },
    }
    job = {"status": "queued", "params": {"config_snapshot": settings["config"]}}
    with backend.transaction() as connection:
        backend.save_settings(settings, connection)
        connection.execute("INSERT INTO projects VALUES(?,?)", (project["id"], json.dumps(project)))
        connection.execute(
            "INSERT INTO jobs(project_id,run_id,arq_job_id,document) VALUES(?,?,?,?)",
            (project["id"], "frozen-run", "frozen-run", json.dumps(job)),
        )
    return backend, settings, project, job


def test_startup_backs_up_global_and_project_settings_once(saved_catalog):
    backend, original_settings, original_project, original_job = saved_catalog
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: LocalBackend(backend.workspace), range(2)))
    settings = backend.load_settings()
    assert settings["revision"] == 6
    assert settings["library_metadata"] == original_settings["library_metadata"]
    assert settings["config"] == {"llm": {"preset": "fake"}}
    with backend.transaction(write=False) as connection:
        project = json.loads(connection.execute("SELECT document FROM projects").fetchone()[0])
        job = json.loads(connection.execute("SELECT document FROM jobs").fetchone()[0])
        backups = connection.execute(
            "SELECT project_id,document FROM configuration_backups ORDER BY id"
        ).fetchall()
    assert project == {
        **original_project,
        "config": {
            "pipeline": {"review": False, "prompt_genre": "academic"},
            "output": {"bilingual": True},
        },
    }
    assert job == original_job
    assert [(pid, json.loads(document)) for pid, document in backups] == [
        (None, original_settings),
        ("synthetic", {"config": original_project["config"], "strategy": None}),
    ]
    LocalBackend(backend.workspace)
    with backend.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM configuration_backups").fetchone()[0] == 2


@pytest.mark.parametrize("preset", ["deepseek", None])
def test_reopening_catalog_removes_old_choices_from_both_model_pickers(tmp_path, preset):
    from fastapi.testclient import TestClient
    from wenyi_desktop.main import create_app, create_context

    workspace = tmp_path / "workspace"
    backend = LocalBackend(workspace)
    pid = backend.create_project("Book", "en", "zh", {"template": "标准翻译"})
    profile = {"provider": "default", "model": "deepseek-flash"}
    names = ["default", "default_strong", "default_cheap", "default_fast"]
    llm = {
        "preset": preset,
        "providers": {"default": {"kind": "deepseek"}},
        "default_model": "default",
        "models": dict.fromkeys(names, profile),
    }
    with backend.transaction() as connection:
        backend.save_settings({"config": {"llm": llm}, "revision": 1}, connection)
    backend.set_project_config(pid, {"llm": {"default_model": "default_fast"}})
    backend.close()

    context = create_context(workspace)
    with TestClient(create_app(context=context)) as client:
        settings = client.get("/settings").json()["effective"]["llm"]
        project = client.get(f"/projects/{pid}/config").json()
        assert list(settings["providers"]) == ["deepseek"]
        assert list(settings["models"]) == ["deepseek/deepseek-flash"]
        assert project["registered_models"] == {
            "deepseek/deepseek-flash": {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "thinking_levels": ["off", "low", "high", "max"],
                "thinking_value": "high",
            }
        }
        assert "default_model" not in project["effective"]["llm"]
        assert {route["model"] for route in project["effective"]["llm"]["routes"].values()} == {
            "deepseek/deepseek-flash"
        }


def test_new_catalog_loads_legacy_bootstrap_defaults_without_rewriting_yaml(tmp_path):
    seed = tmp_path / "config.yaml"
    original = "output: {punctuation_normalize: true, bilingual_order: source_first}\n"
    seed.write_text(original, encoding="utf-8")

    backend = LocalBackend(tmp_path / "workspace", str(seed))
    output = backend.defaults().output

    assert output.bilingual_order == "source_first"
    assert "punctuation_normalize" not in output.model_dump()
    assert seed.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("occupied", [False, True])
def test_rebuilt_preset_keeps_the_vault_reference_without_overwriting_another(tmp_path, occupied):
    from wenyi_desktop.local_credentials import CredentialStore

    backend = LocalBackend(tmp_path / "workspace")
    store = CredentialStore(backend.workspace)
    raw = {
        "preset": "deepseek",
        "providers": {"default": {"kind": "deepseek"}},
        "models": {"default_strong": {"provider": "default", "model": "deepseek-flash"}},
    }
    with backend.transaction() as conn:
        backend.save_settings({"config": {"llm": raw}, "revision": 1}, conn)
        records = {"default": {"mode": "manual", "storage": "system", "reference": "old-ref"}}
        if occupied:
            records["deepseek"] = {**records["default"], "reference": "other-ref"}
        store._commit(records, conn)
    backend.close()
    LocalBackend(backend.workspace).close()
    assert store._records()["deepseek"]["reference"] == ("other-ref" if occupied else "old-ref")
    if not occupied:
        assert "default" not in store._records()


@pytest.mark.parametrize("failure", ["backup", "replacement"])
@pytest.mark.parametrize("scope", ["global", "project"])
def test_backup_and_replacement_roll_back_together(saved_catalog, failure, scope):
    backend, original_settings, original_project, _ = saved_catalog
    if scope == "project":
        original_settings = {**original_settings, "config": {"llm": {"preset": "fake"}}}
        with backend.transaction() as connection:
            backend.save_settings(original_settings, connection)
    statement = (
        "BEFORE INSERT ON configuration_backups"
        if failure == "backup"
        else f"BEFORE UPDATE ON {'application_settings' if scope == 'global' else 'projects'}"
    )
    with backend.transaction() as connection:
        connection.execute(
            f"CREATE TRIGGER fail_rebuild {statement} "
            "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic write failure"):
        LocalBackend(backend.workspace)
    assert backend.load_settings() == original_settings
    with backend.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM configuration_backups").fetchone()[0] == 0
        assert json.loads(connection.execute("SELECT document FROM projects").fetchone()[0]) == (
            original_project
        )


@pytest.mark.parametrize(
    "strategy",
    [
        {"template": "快速出稿"},
        {"template": "标准翻译", "steps": {"report": True}},
        {"template": "removed-template"},
    ],
)
def test_removed_workflow_settings_are_backed_up_and_reset(tmp_path, strategy):
    from wenyi_backend.project_service import effective_config
    from wenyi_core.config import Config

    backend = LocalBackend(tmp_path / "workspace")
    settings = {"config": {"llm": {"preset": "fake"}}, "template": "快速出稿", "revision": 3}
    project = {
        "id": "synthetic",
        "source_lang": "en",
        "target_lang": "zh",
        "strategy": strategy,
        "config": {"pipeline": {"review": False}},
    }
    with backend.transaction() as connection:
        backend.save_settings(settings, connection)
        connection.execute("INSERT INTO projects VALUES(?,?)", (project["id"], json.dumps(project)))

    LocalBackend(backend.workspace)
    assert backend.load_settings() == {**settings, "template": "标准翻译", "revision": 4}
    with backend.transaction(write=False) as connection:
        restored = json.loads(connection.execute("SELECT document FROM projects").fetchone()[0])
        backups = connection.execute(
            "SELECT project_id,document FROM configuration_backups ORDER BY id"
        ).fetchall()
    assert restored == {**project, "strategy": {}}
    assert [(pid, json.loads(document)) for pid, document in backups] == [
        (None, settings),
        (project["id"], {"config": project["config"], "strategy": strategy}),
    ]
    config = effective_config(
        restored, defaults=Config.from_dict({"llm": {"preset": "fake"}}), state_dir="unused"
    )
    assert config.pipeline.book_understanding and config.pipeline.polish
    assert not config.pipeline.review
    LocalBackend(backend.workspace)
    with backend.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM configuration_backups").fetchone()[0] == 2
