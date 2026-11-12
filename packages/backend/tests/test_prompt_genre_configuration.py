"""Project prompt families survive persistence without becoming global Core settings."""

from copy import deepcopy

import pytest
from wenyi_backend.config_documents import (
    config_document,
    config_from_document,
    global_document,
    project_document,
    runtime_document,
)
from wenyi_backend.execution.bindings import build_config_for
from wenyi_backend.global_settings import validate_settings
from wenyi_backend.project_service import effective_config
from wenyi_backend.strategies import strategy_to_config
from wenyi_core.config import Config


def defaults() -> Config:
    return Config.from_dict({"llm": {"preset": "fake"}})


@pytest.mark.parametrize("initialized", [False, True])
def test_saved_academic_project_restores_every_semantic_phase(tmp_path, initialized):
    config = defaults()
    config.set_prompt_genre("academic")
    document = project_document(config)
    assert document["pipeline"]["prompt_genre"] == "academic"
    project = {
        "source_lang": "en",
        "target_lang": "zh",
        "initialized": initialized,
        "config": document,
    }
    before = deepcopy(project)
    restored = effective_config(project, defaults=defaults(), state_dir=str(tmp_path))
    for phase in ("analysis", "translation", "review"):
        assert restored.language_policy(phase).context.genre == "academic"
    assert project == before


def test_old_projects_and_application_defaults_remain_literary(tmp_path):
    config = defaults()
    config.set_prompt_genre("academic")
    restored = effective_config({}, defaults=config, state_dir=str(tmp_path))
    assert restored.language_policy().context.genre == "literary"
    assert "prompt_genre" not in global_document(config)["pipeline"]


@pytest.mark.parametrize("document", [{}, {"pipeline": {"review": False}}])
def test_partial_edits_and_restore_defaults_keep_the_project_genre(tmp_path, document):
    project = {"config": {"pipeline": {"prompt_genre": "academic"}}}
    restored = effective_config(
        project, document=document, defaults=defaults(), state_dir=str(tmp_path)
    )
    assert restored.language_policy().context.genre == "academic"


def test_existing_project_cannot_switch_prompt_families(tmp_path):
    with pytest.raises(ValueError, match="new project.*prompt genre"):
        effective_config(
            {"config": {"pipeline": {"prompt_genre": "academic"}}},
            document={"pipeline": {"prompt_genre": "literary"}},
            defaults=defaults(),
            state_dir=str(tmp_path),
        )


def test_global_yaml_rejects_project_prompt_genre():
    with pytest.raises(ValueError, match="project"):
        validate_settings("llm: {preset: fake}\npipeline: {prompt_genre: academic}", "标准翻译")


@pytest.mark.parametrize("genre", ["literary", "academic"])
def test_execution_snapshots_restore_genre_without_mutating_documents(genre):
    config = defaults()
    config.set_prompt_genre(genre)
    document = runtime_document(config)
    before = deepcopy(document)
    restored = config_from_document(document)
    assert restored.prompt_genre == genre
    assert document == before
    assert (
        restored.language_policy("review").fingerprint
        == config.language_policy("review").fingerprint
    )
    assert "prompt_genre" not in config_document(config)["pipeline"]


@pytest.mark.parametrize("snapshot_genre", [None, "academic"])
def test_workers_use_frozen_genre_not_current_project_or_defaults(backend_context, snapshot_genre):
    config = defaults()
    snapshot = config_document(config)
    if snapshot_genre:
        snapshot["pipeline"]["prompt_genre"] = snapshot_genre
    backend_context.repository.get_project.return_value = {
        "config": {
            "pipeline": {"prompt_genre": "academic" if snapshot_genre is None else "literary"}
        }
    }
    backend_context.repository.get_job_by_arq_id.return_value = {
        "params": {"config_snapshot": snapshot}
    }
    restored = build_config_for(backend_context, "p", "run")
    assert restored.prompt_genre == (snapshot_genre or "literary")
    backend_context.settings_store.load.assert_not_called()


def test_worker_without_snapshot_uses_saved_project_genre(backend_context):
    backend_context.repository.get_project.return_value = {
        "config": {"pipeline": {"prompt_genre": "academic"}}
    }
    backend_context.settings_store.load.return_value.config = defaults()
    assert build_config_for(backend_context, "p").prompt_genre == "academic"


def test_changing_workflow_strategy_preserves_runtime_genre():
    config = defaults()
    config.set_prompt_genre("academic")
    restored = strategy_to_config({"steps": {"review": False}}, config)
    assert restored.prompt_genre == "academic"
    assert restored.pipeline.review is False


@pytest.mark.parametrize("genre", ["", None, "Academic", "../academic"])
def test_corrupt_saved_genres_are_rejected_before_execution(tmp_path, genre):
    with pytest.raises(ValueError, match="prompt genre"):
        effective_config(
            {"config": {"pipeline": {"prompt_genre": genre}}},
            defaults=defaults(),
            state_dir=str(tmp_path),
        )
