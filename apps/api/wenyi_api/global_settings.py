"""Persistent shared model registry and defaults for newly created projects."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from psycopg import Connection
from psycopg.types.json import Jsonb
from wenyi_core.config import Config

from .config import settings
from .config_documents import config_document, merge_project, parse_yaml
from .db import get_pool
from .strategies import PRESET_TEMPLATES


@dataclass(frozen=True)
class GlobalSettings:
    config: Config
    default_template: str = "标准翻译"
    revision: int = 0


def load_settings(*, connection: Connection[Any] | None = None) -> GlobalSettings:
    with nullcontext(connection) if connection is not None else get_pool().connection() as conn:
        row = conn.execute(
            "SELECT document, default_template, revision FROM application_settings WHERE id=1"
        ).fetchone()
    if row is None:
        return GlobalSettings(Config.load(settings.config_path))
    return GlobalSettings(Config.from_dict(row[0]), row[1], row[2])


@contextmanager
def registry_guard(*, exclusive: bool = False):
    """Serialize registry edits against project configuration writes and creation."""
    with get_pool().connection() as conn:
        if exclusive:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('wenyi:settings',0))")
        else:
            conn.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended('wenyi:settings',0))"
            )
        yield conn


def validate_settings(value: str, default_template: str) -> Config:
    if default_template not in {template["name"] for template in PRESET_TEMPLATES}:
        raise ValueError("Unknown default workflow template")
    config = Config.from_dict(parse_yaml(value))
    if config.source_lang == config.target_lang:
        raise ValueError("Source and target languages are identical")
    return config


def save_settings(value: str, default_template: str, revision: int) -> GlobalSettings:
    config = validate_settings(value, default_template)
    document = config_document(config)
    with registry_guard(exclusive=True) as conn:
        current = conn.execute("SELECT revision FROM application_settings WHERE id=1").fetchone()
        if revision != (current[0] if current else 0):
            raise HTTPException(409, "Global settings changed; reload before saving again")
        # Reject removal of models still selected by projects. Jobs retain independent snapshots.
        for pid, saved in conn.execute("SELECT id, config FROM projects").fetchall():
            try:
                Config.from_dict(merge_project(document, {"llm": (saved or {}).get("llm", {})}))
            except ValueError as error:
                raise ValueError(
                    f"Model selections for project {pid} would be invalid: {error}"
                ) from error
        conn.execute(
            """INSERT INTO application_settings(id, document, default_template, revision)
               VALUES(1,%s,%s,%s) ON CONFLICT(id) DO UPDATE
               SET document=EXCLUDED.document, default_template=EXCLUDED.default_template,
                   revision=EXCLUDED.revision, updated_at=now()""",
            (Jsonb(document), default_template, revision + 1),
        )
    return GlobalSettings(config, default_template, revision + 1)


def registered_models(config: Config) -> dict[str, Any]:
    """Expose model choices without provider credentials or connection settings."""
    return {
        key: {"model": profile.model, "provider": profile.provider}
        for key, profile in config.llm.models.items()
    }
