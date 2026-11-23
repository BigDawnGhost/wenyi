"""Offline ports for shared application contract tests."""

import sys
from importlib.util import find_spec
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

# Modules the backend tests import at collection time. A plain `uv sync --locked`
# does not necessarily install all of them, so probe them before importing the
# backend itself and point at the full workspace install when one is missing.
REQUIRED_TEST_MODULES = ("anyio", "fastapi", "httpx2", "starlette", "wenyi_backend")


def missing_test_modules() -> list[str]:
    """Return the backend test dependencies that cannot be imported."""
    missing = []
    for name in REQUIRED_TEST_MODULES:
        try:
            available = find_spec(name) is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            missing.append(name)
    return missing


def require_test_modules() -> None:
    """Stop pytest with install guidance instead of a raw import traceback."""
    missing = missing_test_modules()
    if not missing:
        return
    message = (
        "The backend tests need dependencies that are not installed: "
        + ", ".join(missing)
        + "\nInstall the full workspace development environment first:\n"
        "  uv sync --locked --all-packages --group dev\n"
        "Then rerun pytest from the repository root."
    )
    # A plain SystemExit passes through conftest loading untouched; pytest.exit()
    # would be wrapped into an "ImportError while loading conftest" usage error.
    print(message, file=sys.stderr)
    raise SystemExit(1)


require_test_modules()

from wenyi_backend.context import BackendContext, Telemetry, use_context  # noqa: E402
from wenyi_core.config import Config  # noqa: E402
from wenyi_core.llm.providers.fake import FakeClient  # noqa: E402


@pytest.fixture(autouse=True)
def backend_context(tmp_path):
    repository = Mock()
    context = BackendContext(
        repository=repository,
        settings_store=Mock(defaults=lambda: Config()),
        exports=Mock(),
        telemetry=cast(
            Telemetry,
            SimpleNamespace(
                progress_snapshot=lambda pid: None,
                statistics_snapshot=lambda pid, job: None,
                connect=lambda: Mock(),
                release=lambda cache: None,
            ),
        ),
        storage_for=Mock(),
        read_storage_for=Mock(),
        build_client=lambda config: FakeClient(),
        enqueue=Mock(),
        data_dir=str(tmp_path),
        project_dir=lambda pid: str(tmp_path / pid),
        health=lambda: None,
        update_term=Mock(),
        remove_project=Mock(),
    )
    with use_context(context):
        yield context
