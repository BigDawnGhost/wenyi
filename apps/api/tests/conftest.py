"""Web adapters are owned by each test's explicit application context."""

from dataclasses import replace

import pytest
from wenyi_api.adapters import create_context
from wenyi_api.config import Settings
from wenyi_backend.context import use_context


@pytest.fixture(autouse=True)
def web_context(tmp_path):
    context = create_context(replace(Settings(), data_dir=str(tmp_path), api_token=None))
    with use_context(context):
        yield context
