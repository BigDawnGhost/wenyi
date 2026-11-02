"""Print the API OpenAPI document as canonical JSON, without a running server.

The web end-to-end job needs to know whether the HTTP contract changed: a
route or schema edit can break the browser specs even when no file under
apps/web changed. Deploying a server for that check would cost more than it
saves, so the document is built offline from the same application factory the
tests use and printed in a stable order.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from wenyi_backend.application import create_app
from wenyi_backend.context import BackendContext
from wenyi_core.config import Config


def build_context(data_dir: str) -> BackendContext:
    """Return a context whose ports are inert: schema generation runs no handler."""
    return BackendContext(
        repository=Mock(),
        settings_store=Mock(defaults=lambda: Config()),
        exports=Mock(),
        telemetry=SimpleNamespace(
            progress_snapshot=lambda project_id: None,
            statistics_snapshot=lambda project_id, job: None,
            connect=lambda: Mock(),
            release=lambda cache: None,
        ),
        storage_for=Mock(),
        read_storage_for=Mock(),
        build_client=lambda config: Mock(),
        enqueue=Mock(),
        data_dir=data_dir,
        project_dir=lambda project_id: data_dir,
        health=lambda: None,
        update_term=Mock(),
        remove_project=Mock(),
    )


def main() -> None:
    schema = create_app(build_context(".")).openapi()
    print(json.dumps(schema, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
