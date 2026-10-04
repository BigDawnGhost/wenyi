"""Offline signature checks for the shared repository and its platform adapters."""

import inspect

import pytest
from wenyi_api.adapters import PostgresRepository
from wenyi_api.config import Settings
from wenyi_backend.context import Repository
from wenyi_desktop.local_backend import LocalBackend


def test_admission_contract_has_explicit_keywords_and_positional_job_identity():
    signature = inspect.signature(Repository.admit_job)
    assert signature.parameters["job_id"].kind is inspect.Parameter.POSITIONAL_ONLY
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    signature.bind(
        None,
        "project",
        "translation",
        "job",
        project_status="translating",
        params={},
        config_snapshot={},
        run_id="run",
    )


@pytest.mark.parametrize("platform", ["web", "desktop"])
def test_strategy_and_admission_calls_match_both_platforms(tmp_path, platform):
    local = LocalBackend(tmp_path)
    repository: Repository = local if platform == "desktop" else PostgresRepository(Settings())
    try:
        strategy_args = ("project", {"steps": {}})
        inspect.signature(Repository.set_project_strategy).bind(
            None, *strategy_args, connection=None
        )
        inspect.signature(repository.set_project_strategy).bind(*strategy_args, connection=None)
        inspect.signature(repository.admit_job).bind(
            "project",
            "translation",
            "job",
            project_status="translating",
            params={},
            config_snapshot={},
            run_id="run",
        )
    finally:
        local.close()
