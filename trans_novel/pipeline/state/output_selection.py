"""持久化与输入源绑定的输出选择。"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trans_novel.pipeline.state.store import RunStore

_OUTPUT_SELECTION_FILE = "output_selection.json"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_INVALID = "theme_config: invalid_output_selection"
_UNKNOWN_SCHEMA = "theme_config: unknown_output_selection_schema"
_SOURCE_MISMATCH = "theme_config: source_mismatch"


def _validate_json_value(value: object, seen: set[int]) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise ValueError("non-finite number")
    if isinstance(value, list | dict):
        marker = id(value)
        if marker in seen:
            raise ValueError("cyclic value")
        seen.add(marker)
        try:
            values = value
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("non-string key")
                values = value.values()
            for item in values:
                _validate_json_value(item, seen)
        finally:
            seen.remove(marker)
        return
    raise ValueError("non-JSON value")


class SavedOutputSelection(BaseModel):
    """严格、可持久化的输出选择记录。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    source_bytes_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: dict[str, Any]
    origins: dict[str, str]

    @field_validator("selection", mode="before")
    @classmethod
    def _selection_is_json(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("selection must be an object")
        _validate_json_value(value, set())
        return value

    @field_validator("origins", mode="before")
    @classmethod
    def _origins_are_strings(cls, value: object) -> object:
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(origin, str) for key, origin in value.items()
        ):
            raise ValueError("origins must contain only strings")
        return value


def _path(store: RunStore) -> str:
    return os.path.join(store.run_dir, _OUTPUT_SELECTION_FILE)


def _invalid() -> ValueError:
    return ValueError(_INVALID)


def load_output_selection(
    store: RunStore, *, source_bytes_sha256: str
) -> SavedOutputSelection | None:
    """读取选择；缺失时返回空，损坏或源不匹配时拒绝继续。"""
    if not isinstance(source_bytes_sha256, str) or _HEX64.fullmatch(source_bytes_sha256) is None:
        raise _invalid()
    try:
        payload = store.read_json(_path(store))
    except FileNotFoundError:
        return None
    except (OSError, RecursionError, TypeError, ValueError, UnicodeError):
        raise _invalid() from None
    if not isinstance(payload, dict):
        raise _invalid()
    version = payload.get("schema_version")
    if type(version) is not int:
        raise _invalid()
    if version not in (1, 2):
        raise ValueError(_UNKNOWN_SCHEMA)
    candidate = {**payload, "schema_version": 2} if version == 1 else payload
    try:
        saved = SavedOutputSelection.model_validate(candidate)
    except (RecursionError, TypeError, ValueError):
        raise _invalid() from None
    if saved.source_bytes_sha256 != source_bytes_sha256:
        raise ValueError(_SOURCE_MISMATCH)
    if version == 1:
        store.log_event("output_selection_obsolete", schema_version=1)
        return None
    return saved


def save_output_selection(
    store: RunStore,
    selection: Mapping[str, Any],
    origins: Mapping[str, str],
    *,
    source_bytes_sha256: str,
) -> None:
    """在调用方持有运行锁时原子保存选择。"""
    try:
        saved = SavedOutputSelection(
            source_bytes_sha256=source_bytes_sha256,
            selection=dict(selection),
            origins=dict(origins),
        )
    except (RecursionError, TypeError, ValueError):
        raise _invalid() from None
    load_output_selection(store, source_bytes_sha256=source_bytes_sha256)
    store.write_json(_path(store), saved.model_dump(mode="json"))
