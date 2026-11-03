"""Build the shared HTTP schema offline, without a running server or platform services.

The committed TypeScript contract is derived from here. The module lives in the
package rather than in a script so the schema becomes an importable, type-checked
entry point; `scripts/contract/export_openapi.py` is only a command-line wrapper.
"""

from __future__ import annotations

from typing import Any, NoReturn

from .application import create_app
from .context import BackendContext


class UnavailablePorts:
    """Fail loudly if route assembly reaches for a runtime port.

    Schema generation must describe the contract, not serve it. A port that is
    actually called would make the document depend on a database, a queue or a
    credential store, and could then differ between machines.
    """

    def __getattr__(self, name: str) -> NoReturn:
        raise AssertionError(f"Schema generation accessed a runtime port: {name}")

    def __call__(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("Schema generation called a runtime port")


def build_schema() -> dict:
    """Use the shared route assembly, never a Web/Desktop entry point or lifespan."""
    ports: Any = UnavailablePorts()
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
