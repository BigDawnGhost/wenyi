"""Web reconstruction keeps backups in PostgreSQL, not filesystem state copies."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb
from wenyi_api.config_rebuild import rebuild_saved_settings


@pytest.fixture
def saved_settings(pg_pool):
    pid = uuid.uuid4().hex
    global_config = {"llm": {"preset": "fake"}, "honorific": {"strategy": "drop"}}
    project_config = {
        "pipeline": {
            "translation_mode": "best_of_three",
            "precision_concurrency": 1,
            "review": False,
            "prompt_genre": "academic",
        },
        "output": {"bilingual": True},
    }
    with pg_pool.connection() as connection:
        connection.execute("DELETE FROM application_settings")
        connection.execute("DELETE FROM configuration_backups WHERE project_id IS NULL")
        connection.execute(
            "INSERT INTO application_settings(id,document,default_template,revision) "
            "VALUES(1,%s,'标准翻译',5)",
            (Jsonb(global_config),),
        )
        connection.execute(
            "INSERT INTO projects(id,name,config,usage) VALUES(%s,'test',%s,%s)",
            (pid, Jsonb(project_config), Jsonb({"tokens": 123})),
        )
    try:
        yield pid, global_config, project_config
    finally:
        with pg_pool.connection() as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (pid,))
            connection.execute("DELETE FROM application_settings")
            connection.execute("DELETE FROM configuration_backups WHERE project_id IS NULL")


def test_rebuild_is_transactional_idempotent_and_safe_across_starting_workers(
    pg_pool, saved_settings, tmp_path
):
    pid, global_config, project_config = saved_settings
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _: rebuild_saved_settings(pg_pool, str(tmp_path / "unused.yaml")), range(2)
            )
        )
    with pg_pool.connection() as connection:
        saved_global = connection.execute(
            "SELECT document,revision FROM application_settings WHERE id=1"
        ).fetchone()
        saved_project = connection.execute(
            "SELECT config,usage FROM projects WHERE id=%s", (pid,)
        ).fetchone()
        backups = connection.execute(
            "SELECT project_id,document FROM configuration_backups ORDER BY id"
        ).fetchall()
    assert saved_global == ({"llm": {"preset": "fake"}}, 6)
    assert saved_project == (
        {
            "pipeline": {"review": False, "prompt_genre": "academic"},
            "output": {"bilingual": True},
        },
        {"tokens": 123},
    )
    assert backups == [
        (None, {"config": global_config, "default_template": "标准翻译", "revision": 5}),
        (pid, {"config": project_config, "strategy": None}),
    ]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["backup", "replacement"])
@pytest.mark.parametrize("scope", ["global", "project"])
def test_backup_and_replacement_failures_roll_back_original_settings(
    pg_pool, saved_settings, tmp_path, failure, scope
):
    pid, global_config, project_config = saved_settings
    table = (
        "configuration_backups"
        if failure == "backup"
        else "application_settings"
        if scope == "global"
        else "projects"
    )
    event = "INSERT" if failure == "backup" else "UPDATE"
    with pg_pool.connection() as connection:
        if scope == "project":
            global_config = {"llm": {"preset": "fake"}}
            connection.execute(
                "UPDATE application_settings SET document=%s WHERE id=1", (Jsonb(global_config),)
            )
        connection.execute(
            """CREATE FUNCTION fail_config_rebuild() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN RAISE EXCEPTION 'synthetic write failure'; END $$"""
        )
        connection.execute(
            f"CREATE TRIGGER fail_rebuild BEFORE {event} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION fail_config_rebuild()"
        )
    try:
        with pytest.raises(RaiseException, match="synthetic write failure"):
            rebuild_saved_settings(pg_pool, str(tmp_path / "unused.yaml"))
        with pg_pool.connection() as connection:
            assert connection.execute(
                "SELECT document,revision FROM application_settings WHERE id=1"
            ).fetchone() == (global_config, 5)
            assert (
                connection.execute("SELECT config FROM projects WHERE id=%s", (pid,)).fetchone()[0]
                == project_config
            )
            assert (
                connection.execute("SELECT count(*) FROM configuration_backups").fetchone()[0] == 0
            )
    finally:
        with pg_pool.connection() as connection:
            connection.execute(f"DROP TRIGGER fail_rebuild ON {table}")
            connection.execute("DROP FUNCTION fail_config_rebuild()")


def test_read_only_bootstrap_yaml_is_backed_up_in_database(pg_pool, tmp_path):
    path = tmp_path / "config.yaml"
    original = "llm: {preset: fake}\nhonorific: {strategy: drop}\n"
    path.write_text(original, encoding="utf-8")
    with pg_pool.connection() as connection:
        connection.execute("DELETE FROM application_settings")
        connection.execute("DELETE FROM configuration_backups WHERE project_id IS NULL")
    try:
        rebuild_saved_settings(pg_pool, str(path))
        assert path.read_text(encoding="utf-8") == original
        assert list(tmp_path.iterdir()) == [path]
        with pg_pool.connection() as connection:
            assert connection.execute(
                "SELECT document FROM application_settings WHERE id=1"
            ).fetchone()[0] == {"llm": {"preset": "fake"}}
            backup = connection.execute(
                "SELECT document FROM configuration_backups WHERE project_id IS NULL"
            ).fetchone()[0]
            assert backup["config"]["honorific"] == {"strategy": "drop"}
    finally:
        with pg_pool.connection() as connection:
            connection.execute("DELETE FROM application_settings")
            connection.execute("DELETE FROM configuration_backups WHERE project_id IS NULL")


@pytest.mark.parametrize(
    "strategy",
    [
        {"template": "快速出稿"},
        {"template": "标准翻译", "steps": {"report": True}},
        {"template": "removed-template"},
    ],
)
def test_removed_workflow_settings_are_backed_up_and_reset(
    pg_pool, saved_settings, tmp_path, strategy
):
    pid, _, _ = saved_settings
    config = {"llm": {"preset": "fake"}}
    project_config = {"pipeline": {"review": False}}
    with pg_pool.connection() as connection:
        connection.execute(
            "UPDATE application_settings SET document=%s,default_template=%s WHERE id=1",
            (Jsonb(config), "快速出稿"),
        )
        connection.execute(
            "UPDATE projects SET config=%s,strategy=%s WHERE id=%s",
            (Jsonb(project_config), Jsonb(strategy), pid),
        )

    rebuild_saved_settings(pg_pool, str(tmp_path / "unused.yaml"))
    rebuild_saved_settings(pg_pool, str(tmp_path / "unused.yaml"))
    with pg_pool.connection() as connection:
        assert connection.execute(
            "SELECT document,default_template,revision FROM application_settings WHERE id=1"
        ).fetchone() == (config, "标准翻译", 6)
        assert connection.execute(
            "SELECT config,strategy,usage FROM projects WHERE id=%s", (pid,)
        ).fetchone() == (project_config, {}, {"tokens": 123})
        assert connection.execute(
            "SELECT project_id,document FROM configuration_backups ORDER BY id"
        ).fetchall() == [
            (None, {"config": config, "default_template": "快速出稿", "revision": 5}),
            (pid, {"config": project_config, "strategy": strategy}),
        ]
