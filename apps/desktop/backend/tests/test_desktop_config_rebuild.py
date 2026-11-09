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
