"""Offline ports for shared application contract tests."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from wenyi_backend.context import BackendContext, use_context
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient


@pytest.fixture(autouse=True)
def backend_context(tmp_path):
    repository = Mock()
    context = BackendContext(
        repository=repository,
        settings_store=Mock(defaults=lambda: Config()),
        exports=Mock(),
        telemetry=SimpleNamespace(
            progress_snapshot=lambda pid: None,
            statistics_snapshot=lambda pid, job: None,
            connect=lambda: Mock(),
            release=lambda cache: None,
        ),
        storage_for=Mock(),
        read_storage_for=Mock(),
        build_client=lambda config: FakeClient(),
        enqueue=Mock(),
        data_dir=str(tmp_path),
        project_dir=lambda pid: str(tmp_path / pid),
        health=lambda: None,
        update_term=Mock(),
    )
    with use_context(context):
        yield context
