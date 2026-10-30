"""Keep offline guards compatible with Desktop-only and full workspace installs."""

from importlib import import_module
from importlib.util import find_spec

import keyring
import pytest


@pytest.mark.parametrize("name", ["get_password", "set_password", "delete_password", "get_keyring"])
def test_vault_calls_are_forbidden(name):
    # This must also reach the test body when no Web dependencies are installed.
    with pytest.raises(AssertionError, match="Offline Desktop"):
        getattr(keyring, name)()


def test_installed_web_service_calls_are_forbidden():
    calls = {
        "psycopg": ["connect"],
        "psycopg_pool": ["ConnectionPool"],
        "redis": ["from_url", "Redis.from_url", "connection.Connection.connect"],
        "redis.asyncio": ["Redis.from_url", "connection.Connection.connect"],
    }
    for module_name, paths in calls.items():
        if find_spec(module_name.split(".")[0]) is None:
            continue
        module = import_module(module_name)
        for path in paths:
            call = module
            for name in path.split("."):
                call = getattr(call, name)
            # Guards are synchronous, including the async Redis entry points:
            # no socket, coroutine, pool worker, or real vault should be reached.
            with pytest.raises(AssertionError, match="Offline Desktop"):
                call()
