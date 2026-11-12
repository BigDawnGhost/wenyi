"""Offline call-shape checks for the shared repository protocol."""

import inspect

from wenyi_backend.context import Repository


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
    inspect.signature(Repository.set_project_strategy).bind(
        None, "project", {"steps": {}}, connection=None
    )
