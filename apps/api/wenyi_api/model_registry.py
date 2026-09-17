"""Rename registered model references without changing frozen job configurations."""

from __future__ import annotations

from copy import deepcopy

from psycopg import Connection
from wenyi_core.config import Config

from .config_documents import config_document, merge_project


def rename_model_references(llm: dict, renames: dict[str, str]) -> dict:
    result = deepcopy(llm)
    if "tiers" in result:
        result["tiers"] = {
            tier: renames.get(model, model) for tier, model in result["tiers"].items()
        }
    for route in result.get("routes", {}).values():
        if "model" in route:
            route["model"] = renames.get(route["model"], route["model"])
        if "fallbacks" in route:
            route["fallbacks"] = [renames.get(model, model) for model in route["fallbacks"]]
    return result


def project_registry_updates(
    connection: Connection, current: Config, proposed: Config, renames: dict[str, str]
) -> list[tuple[str, dict]]:
    if len(set(renames.values())) != len(renames):
        raise ValueError("Renamed model IDs must be unique")
    for old, new in renames.items():
        if old not in current.llm.models:
            raise ValueError(f"Cannot rename model {old!r}: the original ID is not registered")
        if new not in proposed.llm.models or new in current.llm.models:
            raise ValueError(f"Cannot rename model to {new!r}: use a new registered ID")
    document = config_document(proposed)
    updates = []
    for pid, saved in connection.execute("SELECT id, config FROM projects").fetchall():
        saved = saved or {}
        selections = rename_model_references(saved.get("llm", {}), renames)
        try:
            Config.from_dict(merge_project(document, {"llm": selections}))
        except ValueError as error:
            raise ValueError(
                f"Model selections for project {pid} would be invalid: {error}"
            ) from error
        if selections != saved.get("llm", {}):
            updates.append((pid, {**saved, "llm": selections}))
    return updates
