"""Rebuild saved settings from current schemas without interpreting retired options.

New configuration submissions still use strict validation. Callers must durably
back up the original document before persisting a changed reconstruction.
"""

from __future__ import annotations

import hashlib
import logging
import stat
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from .atomicio import publish_target
from .config import CONFIG_SECTIONS, Config, parse_config_yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RebuiltConfig:
    document: dict[str, Any]
    removed: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.removed)


def merge_section(name: str, base: dict, override: dict) -> dict:
    """Match project override semantics, including partial tier selections."""
    merged = {**base, **override}
    if name == "llm" and isinstance(override.get("tiers"), dict):
        merged["tiers"] = {**base.get("tiers", {}), **override["tiers"]}
    return merged


def _remove_location(value: dict, location: tuple) -> tuple | None:
    """Remove the closest supplied ancestor when validation reports a missing child."""
    current: Any = value
    ancestors = []
    for part in location:
        if isinstance(current, dict) and part in current:
            ancestors.append((current, part))
            current = current[part]
        elif isinstance(current, list) and isinstance(part, int) and 0 <= part < len(current):
            ancestors.append((current, part))
            current = current[part]
        else:
            break
    if not ancestors:
        return None
    parent, key = ancestors[-1]
    del parent[key]
    return location[: len(ancestors)]


def _rebuild_section(
    name: str, model: type[BaseModel], value: dict, base: dict
) -> tuple[dict, list[str]]:
    candidate = deepcopy(value)
    removed = []
    while True:
        try:
            model.model_validate(merge_section(name, base, candidate))
            return candidate, removed
        except ValidationError as error:
            locations = [tuple(item["loc"]) for item in error.errors(include_input=False)]
        for location in locations:
            deleted = _remove_location(candidate, location)
            if deleted is not None:
                removed.append(".".join((name, *map(str, deleted))))
                break
        else:
            # Once a broken registry loses its preset or required tiers, restore
            # the built-in registry without throwing away independent valid budgets.
            if name == "llm" and not base and candidate.get("preset") is None:
                preset = model.model_validate({}).model_dump()["preset"]
                defaulted = {**candidate, "preset": preset}
                try:
                    model.model_validate(defaulted)
                except ValidationError:
                    pass
                else:
                    return defaulted, [*removed, f"{name}.preset"]
            # Cross-field validators may identify a whole dependency group. Prefer
            # dropping one conflicting supplied field if that leaves a valid group.
            # Schema order keeps YAML and JSONB key ordering from changing the outcome.
            for key in model.model_fields:
                if key not in candidate:
                    continue
                reduced = {field: item for field, item in candidate.items() if field != key}
                try:
                    model.model_validate(merge_section(name, base, reduced))
                except ValidationError:
                    continue
                removed.append(f"{name}.{key}")
                return reduced, removed
            # Do not invent replacements or leave dangling provider/model references.
            model.model_validate(base)
            return {}, [*removed, name]


def rebuild_config(raw: Any, *, base: dict | None = None) -> RebuiltConfig:
    """Keep valid supplied fields; inherit defaults rather than aliasing obsolete values."""
    if not isinstance(raw, dict):
        return RebuiltConfig({}, ("configuration",))
    defaults = base or {}
    document = {}
    removed = []
    for name, value in raw.items():
        model = CONFIG_SECTIONS.get(name)
        if model is None or not isinstance(value, dict):
            removed.append(str(name))
            continue
        section, discarded = _rebuild_section(name, model, value, defaults.get(name, {}))
        document[name] = section
        removed.extend(discarded)
    effective = deepcopy(defaults)
    for name, value in document.items():
        effective[name] = merge_section(name, effective.get(name, {}), value)
    Config.from_dict(effective)
    return RebuiltConfig(document, tuple(dict.fromkeys(removed)))


def _publish_config(target: Path, data: bytes, mode: int) -> None:
    """Preserve access permissions before writing any configuration bytes."""
    with publish_target(target) as temporary:
        with temporary.open("xb") as handle:
            temporary.chmod(mode)
            handle.write(data)


def load_rebuilt_config(path: str | Path) -> Config:
    """Upgrade a CLI YAML file only after an exact-byte, content-addressed backup."""
    target = Path(path)
    data = target.read_bytes()
    raw = parse_config_yaml(data.decode("utf-8"))
    rebuilt = rebuild_config({} if raw is None else raw)
    if not rebuilt.changed:
        return Config.from_dict(rebuilt.document)

    from .storage.locks import exclusive_file_lock

    with exclusive_file_lock(target.with_name(f".{target.name}.upgrade.lock"), timeout_ms=5000):
        # Another command may have repaired or edited the file while this one waited.
        data = target.read_bytes()
        raw = parse_config_yaml(data.decode("utf-8"))
        rebuilt = rebuild_config({} if raw is None else raw)
        if rebuilt.changed:
            digest = hashlib.sha256(data).hexdigest()
            mode = stat.S_IMODE(target.stat().st_mode)
            backup = target.with_name(f"{target.name}.backup-{digest}.yaml")
            if backup.exists():
                if backup.read_bytes() != data:
                    raise ValueError(f"Configuration backup is damaged: {backup}")
            else:
                _publish_config(backup, data, mode)
            if target.read_bytes() != data:
                raise ValueError(
                    "Configuration changed during upgrade; retry after editing finishes"
                )
            _publish_config(
                target,
                yaml.safe_dump(rebuilt.document, allow_unicode=True, sort_keys=False).encode(
                    "utf-8"
                ),
                mode,
            )
            logger.warning(
                "Rebuilt saved configuration; discarded %d incompatible fields. Backup: %s",
                len(rebuilt.removed),
                backup,
            )
    return Config.from_dict(rebuilt.document)
