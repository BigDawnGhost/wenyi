"""Export the shared HTTP schema without starting an app or accessing platform services."""

from __future__ import annotations

import json
import sys
from typing import Any, NoReturn

from wenyi_backend.application import create_app
from wenyi_backend.context import BackendContext


class _UnavailablePorts:
    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"Schema generation accessed a runtime port: {name}")

    def __call__(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Schema generation called a runtime port")


def build_schema() -> dict:
    """Use the shared route assembly, never a Web/Desktop entry point or lifespan."""
    ports: Any = _UnavailablePorts()
    context = BackendContext(
        repository=ports,
        settings_store=ports,
        exports=ports,
        telemetry=ports,
        storage_for=ports,
        read_storage_for=ports,
        build_client=ports,
        enqueue=ports,
        data_dir="",
        project_dir=ports,
        health=ports,
        update_term=ports,
        remove_project=ports,
    )
    schema = create_app(context).openapi()
    # The installed release version is not part of the generated type contract.
    schema["info"]["version"] = "0.0.0"
    return schema


if __name__ == "__main__":
    json.dump(build_schema(), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
